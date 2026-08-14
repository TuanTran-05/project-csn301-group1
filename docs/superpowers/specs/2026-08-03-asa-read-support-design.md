# Cisco ASA Read Support — Design Spec

**Date:** 2026-08-03
**Status:** Approved for planning

## Goal

Make FW-01, the lab's Cisco ASA, answer read-only questions through the
copilot like the eight IOS devices already do, so the system monitors 9/9
devices instead of 8/9 plus one that returns an error.

## Motivating evidence

Asking the copilot *"kiem tra ket noi fw01"* produced a result labelled
`SUCCESS` whose body was:

```
show ip interface brief
                 ^
ERROR: % Invalid input detected at '^' marker.
FW-01#
```

SSH itself was fine — 535 ms, prompt returned. The command was simply not
ASA syntax. ASA 9.5(2) uses a different vocabulary:

| Intent | IOS (what is sent today) | ASA (correct) |
|---|---|---|
| Interface state | `show ip interface brief` | `show interface ip brief` |
| Routing table | `show ip route` | `show route` |
| Access lists | `show access-lists` | `show access-list` |

## What was measured, not assumed

Real output was captured from FW-01 (ASA 9.5(2)204) and both existing
parsers were run against it.

**`show interface ip brief` — format is identical to IOS.**
`parse_ip_interface_brief` handled all rows correctly, including
`unassigned` addresses and the two-word `administratively down` status.
It is reused unchanged.

**`show route` — format differs, and the IOS parser corrupts it silently.**
ASA prints a netmask where IOS prints a prefix length, and uses `nameif`
names (`OUTSIDE`, `INSIDE`, `DMZ`, `MGMT`) where IOS prints physical
interface names:

```
S*       0.0.0.0 0.0.0.0 [1/0] via 10.255.0.1, OUTSIDE
C        10.10.10.0 255.255.255.0 is directly connected, MGMT
L        10.10.10.3 255.255.255.255 is directly connected, MGMT
C        10.10.100.0 255.255.255.0 is directly connected, DMZ
C        10.255.0.4 255.255.255.252 is directly connected, INSIDE
```

Feeding that to `parse_ip_routes` returns:

```python
{'network': '10.10.10.0/32',  'interface': None, ...}   # should be /24, MGMT
{'network': '10.10.100.0/32', 'interface': None, ...}   # should be /24, DMZ
{'network': '0.0.0.0/32',     'interface': None, ...}   # should be 0.0.0.0/0
```

No exception, no warning — just wrong data. The IOS parser reads the
netmask as trailing prose and falls back to `/32`, and its interface
regex requires a digit, which `MGMT` and `DMZ` do not have.

## A security interaction worth recording

The [network context spec](2026-08-03-ai-network-context-design.md) filters
out any subnet that *contains* a `Device.management_ip`. A subnet
mis-parsed as `10.10.10.0/32` does **not** contain `10.10.10.3`, so that
filter would not drop it, and the management network could reach the
model.

This cannot happen today: that spec builds `networks` only from `Vlan<N>`
SVIs (an ASA has none) and gathers `routing` only from `core` and
`distribution` roles (FW-01 is `firewall`), so FW-01 is excluded
structurally before any filter runs. It is recorded here because the two
features touch the same data, and widening either boundary later would
make the corruption load-bearing.

Fixing the parser removes the hazard at its source rather than relying on
that structural exclusion holding forever.

## Non-goals

- **Configuring** an ASA. The README already declares ASA configuration
  Preview-only/out of scope, and this spec does not change that: it adds
  read paths only.
- Threading `device_type` into the command policy engine. See the design
  note below — that turned out to be the wrong layer.
- `show vlan brief` for ASA. An ASA has no IOS-style VLAN database, and
  `firewall` is not in monitoring's `SWITCHING_ROLES`, so nothing polls
  for it.
- ASA-specific commands with no IOS counterpart (`show nameif`,
  `show conn`, `show xlate`). Nothing in the current scenarios needs them.

## Design

### 1. The policy engine does not need `device_type`

The first diagnosis assumed `evaluate(command, device_role)` had to learn
about device types. On reflection that is the wrong layer. The policy
engine answers *"is this command read-only and safe?"* — and `show route`
is read-only and safe regardless of what it runs against. Whether a
device *understands* the syntax is a correctness question, and correctness
belongs to the context and prompt.

So the allowlist simply gains three entries, ungated:

```
show interface ip brief
show route
show access-list
```

An IOS device asked for `show route` returns its own syntax error, which
is exactly the symmetric case of what happens today and is no worse. The
prompt (part 4) is what stops the model from doing that.

This keeps the change out of the safety-critical policy code entirely.

### 2. `parsers/asa_routes.py`

A new `parse_asa_routes(raw)` returning the same shape as
`parse_ip_routes` — `{network, protocol, next_hop, interface, distance,
metric}` — so every existing consumer (the chat result table, monitoring
snapshots) works unchanged.

Differences from the IOS parser:

- **Netmask to CIDR**: match `<network> <netmask>` as two dotted quads and
  convert with `ipaddress.IPv4Network(f"{network}/{netmask}",
  strict=False)`. `0.0.0.0 0.0.0.0` becomes `0.0.0.0/0`;
  `10.10.10.3 255.255.255.255` becomes `10.10.10.3/32`.
- **Interface is a `nameif`**: take the token after the final comma
  verbatim. No digit is required, unlike the IOS interface pattern.
- Header noise (`Codes:`, `Gateway of last resort`, and the multi-line
  legend) is skipped the same way the IOS parser skips it: lines that do
  not match the route shape are ignored.

Registered in `parsers/__init__.py` under `show route`, so
`parse_command_output("show route", ...)` routes to it. `show ip route`
continues to map to the IOS parser. The two never see each other's output
again.

### 3. Monitoring selects commands by device type

`monitoring/service.py` currently exposes `commands_for_role(role)`,
built from `BASE_COMMANDS` plus role-driven additions. It becomes
`commands_for_device(device)`, which picks the base command set by
`device.device_type` before applying the existing role rules:

- `cisco_asa` → `["show interface ip brief", "show route"]`
- anything else → today's `["show ip interface brief", "show ip route"]`

The role-driven additions (`show ip ospf neighbor`, `show vlan brief`,
`show interfaces trunk`, `show ip dhcp pool`) are unchanged and, because
`firewall` is in none of those role sets, none of them are added for
FW-01.

This is the one place that genuinely needs `device_type`, because it
chooses commands without a model in the loop.

### 4. Teaching the model ASA syntax

`build_context()` already sends each device's `device_type`, so the model
can already tell FW-01 apart — it just has no idea the vocabulary
differs. Context gains one compact mapping:

```json
"asa_command_equivalents": {
  "show ip interface brief": "show interface ip brief",
  "show ip route": "show route",
  "show access-lists": "show access-list"
}
```

And `SYSTEM_PROMPT` gains one rule: for a device whose `device_type` is
`cisco_asa`, use the ASA equivalent from `asa_command_equivalents`, never
the IOS form.

## Testing

TDD as used throughout this project. The fixtures are the real captured
FW-01 output above, not invented samples.

**`parse_asa_routes`:**
- `C 10.10.10.0 255.255.255.0 is directly connected, MGMT` yields
  `network="10.10.10.0/24"`, `protocol="C"`, `interface="MGMT"`.
- `C 10.255.0.4 255.255.255.252 ... INSIDE` yields a `/30`.
- `S* 0.0.0.0 0.0.0.0 [1/0] via 10.255.0.1, OUTSIDE` yields
  `network="0.0.0.0/0"`, `next_hop="10.255.0.1"`, `distance=1`,
  `metric=0`, `interface="OUTSIDE"`.
- `L 10.10.10.3 255.255.255.255 ... MGMT` yields a `/32` — local host
  routes are parsed, not dropped; callers decide what to do with them.
- The `Codes:` legend and `Gateway of last resort` lines produce no rows.
- Empty/None input returns `[]`.

**Regression on the IOS parser:** the existing `show ip route` tests must
still pass unchanged — this change must not touch IOS behavior.

**Routing table:** `parse_command_output("show route", ...)` reaches the
ASA parser and `parse_command_output("show ip route", ...)` reaches the
IOS one.

**Interface reuse:** `parse_ip_interface_brief` against the captured ASA
output yields the three rows measured above, including `unassigned` and
`administratively down`. This pins the reuse decision so a future edit to
the IOS parser cannot silently break ASA.

**`commands_for_device`:** a `cisco_asa` device gets the ASA base
commands and none of the role extras; an IOS `distribution` device gets
today's exact list.

**Policy:** the three new commands evaluate as allowed read-only
commands.

**Prompt:** `SYSTEM_PROMPT` contains the ASA syntax rule, asserted as a
string so it cannot be dropped unnoticed.

**Live-lab verification:** ask the copilot *"kiem tra ket noi fw01"* and
confirm it now returns a real interface table instead of
`ERROR: % Invalid input detected`. This is the acceptance criterion.

## Rollout

Backend-only and additive: no migration, no new dependency (`ipaddress`
is standard library), no frontend change. `git pull` → restart Flask.

Existing FW-01 snapshots hold error text rather than data; they are
overwritten by the next monitoring poll once the correct commands are in
use. No cleanup step is needed.
