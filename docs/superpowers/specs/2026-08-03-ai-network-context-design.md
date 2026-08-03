# AI Network Context — Design Spec

**Date:** 2026-08-03
**Status:** Approved for planning

## Goal

Let a user who does not know the network's addressing express intent in
plain language — "chặn guest ping tới IT" — and get a correct, applied
configuration, instead of having to spell out subnets, ICMP echo
semantics, interface names and directions themselves.

## Motivating evidence

Observed in the CSN301 lab on 2026-08-03, both runs recorded as batches:

**Batch #17**, from the plain request *"chan guest ping toi it nhung it co
the ping toi guest"*:

```
ip access-list extended GUEST_TO_INTERNAL
 deny ip any any log
ip access-list extended INTERNAL_TO_GUEST
 permit ip any any
```

Reported `success` because IOS accepted the syntax, yet it blocked
nothing: no `ip access-group` was ever emitted, so neither list was
attached to an interface. Had one been attached, `deny ip any any` would
have dropped every packet through the router — OSPF, DHCP relay and all
inter-VLAN traffic.

**Batch #19**, from a request that spelled out the subnets, the `echo`
keyword, the interface and the direction: correct on the first attempt.
GUEST→IT now returns `ICMP type:3, code:13, Communication
administratively prohibited`, while IT→GUEST still succeeds.

The gap between those two runs is the problem this spec addresses. A tool
that only works when the operator already knows the answer offers the
operator nothing.

## Root cause

Two independent causes, both confirmed by reading the code:

1. **The model is never told the network exists.** `AIService.build_context()`
   sends exactly this per device: `hostname`, `role`, `device_type`,
   `status` — plus the read-only command allowlist. No address, no subnet,
   no VLAN. The model cannot map "guest" to `10.10.60.0/24` because it has
   never been told `10.10.60.0/24` is a thing. `deny ip any any` was not
   incompetence; it was the only shape available to it.

2. **The system prompt carries no configuration domain knowledge.** Nothing
   tells the model that an access list is inert until applied with
   `ip access-group`, or that denying all ICMP in one direction also kills
   the echo-reply of the permitted direction.

Fixing only (1) still yields unattached ACLs. Fixing only (2) yields
correctly-shaped ACLs about subnets the model has to guess. Both are in
scope, confirmed during brainstorming.

## Non-goals

- Live SSH queries at chat time to build the map. Every chat turn would
  open sessions to several devices, adding seconds of latency and failing
  whenever a device is down.
- A hand-maintained static topology file. Accurate on the day it is
  written, silently wrong the first time the network changes — the
  opposite of what an automation project should demonstrate.
- Backend validation that warns when a batch creates an access list
  without applying it. Genuinely useful and directly aligned with the
  safety goal, but it modifies `changes/`, the most safety-critical module
  in the codebase, and deserves its own design round rather than being
  folded in here.
- Any change to the policy engine, the approval workflow, or the frontend.
- Teaching the model NAT, ASA syntax, or multi-area OSPF.

## Data source

Everything needed is already collected and parsed. The monitoring
scheduler polls, per role (`monitoring/service.py`):

| Command | Polled on | Parsed shape |
|---|---|---|
| `show ip interface brief` | every device | `{interface, ip_address, status, protocol}` |
| `show ip route` | every device | `{network, protocol, next_hop, interface, distance, metric}` |
| `show vlan brief` | `access`, `distribution` | `{vlan_id, name, status, ports}` |

Results land in `DeviceSnapshot.parsed_data`, keyed by command string. The
distribution switches receive all three, and that is exactly where the
SVIs live, so the join below has the data it needs. (`core` is not polled
for VLANs, which is correct — INTERNAL-RTR is a router and has none.)

## The join

A new module, `ai/topology.py`, exposes one function,
`build_topology() -> dict`, reading each device's latest snapshot.

**`networks`** — one entry per SVI that has an address:

For each interface whose name matches `Vlan<N>` and whose `ip_address` is
a real address (not `unassigned`):

- `vlan_id` = `N`
- `name` = the `name` for that `vlan_id` in the same device's
  `show vlan brief` (omitted when the device was not polled for VLANs)
- `gateway` = the interface's `ip_address`
- `gateway_device` = the device hostname
- `gateway_interface` = the interface name
- `subnet` = the `network` of that device's connected route
  (`protocol == "C"`) whose `interface` equals the interface name

The subnet must come from the route table because `show ip interface
brief` reports an address without a mask; the route table is the only
polled source that carries the prefix length.

Local host routes (`protocol == "L"`, the `/32` entries) are ignored.

Entries are per device per SVI, so a VLAN configured with an SVI on two
devices yields two entries. That is accurate rather than a duplicate — it
is genuinely two gateways — and the model needs both to place a rule
correctly. `networks` is sorted by `vlan_id`, then `gateway_device`, so
the payload is deterministic and testable.

**`routing`** — for devices whose role is `core` or `distribution`, the
routes that lead to the networks above:

```json
{"device": "INTERNAL-RTR",
 "routes": [{"network": "10.10.60.0/24", "interface": "GigabitEthernet0/2",
             "protocol": "O"}]}
```

This is what makes ACL placement derivable: it is how the model learns
that guest traffic reaches INTERNAL-RTR on `Gi0/2`. Routes to anything
not in `networks` — transit `/30` links, the default route — are dropped
as noise.

Resulting shape:

```json
{"networks": [
   {"vlan_id": 60, "name": "GUEST", "subnet": "10.10.60.0/24",
    "gateway": "10.10.60.1", "gateway_device": "DIST-SW2",
    "gateway_interface": "Vlan60"}],
 "routing": [
   {"device": "INTERNAL-RTR",
    "routes": [{"network": "10.10.60.0/24",
                "interface": "GigabitEthernet0/2", "protocol": "O"}]}]}
```

`build_context()` gains a `"topology"` key holding this object.

## Security boundary

`ai/service.py`'s module docstring currently states:

> The model never receives credentials, management IPs or a full
> running-config.

This spec narrows that rule rather than abandoning it. The revised rule:
the model never receives **credentials**, **management IPs**, or a **full
running-config**; it *does* receive user network subnets and their
gateways, because those are what policy reasoning is *about*, whereas a
management IP is a way to *reach* a device.

**The filter is data-driven, never hardcoded.** Collect every
`Device.management_ip` in the inventory; drop any network whose subnet
contains any of them, using `ipaddress` from the standard library. The
lab's `10.10.10.0/24` therefore disappears without that string appearing
anywhere in the code, and the filter keeps working if the management
range is ever renumbered.

Because `networks` is built only from `Vlan<N>` SVIs, transit links and
physical management interfaces are structurally excluded before the
filter even runs; the filter is the second line of defence, not the only
one.

The docstring is updated in the same change, so the stated rule and the
code do not drift apart.

## System prompt additions

Four rules, each corresponding to an error actually observed:

1. Resolve names to addresses using `topology.networks`. When the user
   names a network ("guest", "IT", "VLAN 60"), use its `subnet` — never
   `any` — and use `topology.routing` to choose the interface and
   direction.
2. An access list does nothing until it is applied with
   `ip access-group <name> in|out` on an interface. A proposal that
   creates a list without applying it is incomplete.
3. End an extended access list with `permit ip any any` unless the user
   explicitly asks to block everything else: the implicit `deny any`
   otherwise drops routing protocols and DHCP relay.
4. To block ping in one direction only, deny `icmp ... echo`. Denying all
   `icmp` also drops the echo-reply of the direction meant to keep
   working.

## Degradation

If no snapshot exists — `MONITORING_ENABLED` is `false`, which is the
default and is what `backend/.env` currently sets locally — `networks`
and `routing` come back empty and the model behaves exactly as it does
today: no better, no worse, and no crash.

This is a real operational dependency, not a theoretical one: the feature
only delivers its value on a deployment where the monitoring scheduler is
running. The rollout section records this.

## Testing

TDD as used throughout this project.

**Security** (the most important test in this change): seed devices whose
`management_ip` values sit inside a polled connected network, snapshot
both that network and ordinary user networks, run a chat turn, and assert
no management IP appears anywhere in what reached the provider — using
the existing `FakeAIProvider.everything_sent()` helper, the same
mechanism the credential-hygiene tests already use.

**The join:**
- A distribution switch with `Vlan60` addressed `10.10.60.1`, a
  `show vlan brief` naming VLAN 60 `GUEST`, and a connected route
  `10.10.60.0/24` via `Vlan60` produces exactly one `networks` entry with
  all six fields correct.
- An SVI with no address is skipped.
- An SVI with no matching connected route is skipped (no subnet can be
  determined, and a half-populated entry would mislead the model).
- Local `/32` routes never become a subnet.
- A device polled without `show vlan brief` still yields an entry, with
  `name` absent rather than the entry dropped.
- `routing` includes only `core`/`distribution` devices, and only routes
  whose network appears in `networks`.

**Degradation:** with no snapshots at all, `build_topology()` returns
empty lists and `build_context()` still returns a well-formed dict.

**Prompt:** assert the four rules above are present in `SYSTEM_PROMPT`, so
they cannot be dropped unnoticed — the same style of assertion already
used for the stale-status rule.

**Regression:** the full suite is re-run; `build_context()` gains a key
but changes nothing existing.

**Live-lab verification**, on a deployment with monitoring enabled: issue
the original plain request, *"chan guest ping toi it nhung it co the ping
toi guest"*, and confirm the preview now contains the specific subnets,
the `echo` keyword, a `permit ip any any`, and an `ip access-group` on the
correct interface — the four things Batch #17 lacked. This is the
acceptance criterion for the whole feature; unit tests cannot prove it
because they do not exercise a real model.

## Rollout

Backend-only, additive: no migration, no new dependency (`ipaddress` is
standard library), no frontend change. Deployment is unchanged:
`git pull` → restart Flask.

One operational step is required for the feature to do anything:
`MONITORING_ENABLED=true` in the AI Server's `.env`, so snapshots exist to
build the map from. Without it the change is inert but harmless.
