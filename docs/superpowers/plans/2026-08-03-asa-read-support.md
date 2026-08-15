# Cisco ASA Read Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make FW-01 (Cisco ASA 9.5) answer read-only questions through the copilot like the eight IOS devices already do, so the system monitors 9/9 devices instead of 8/9 plus one that returns `ERROR: % Invalid input detected`.

**Architecture:** ASA differs from IOS only in command vocabulary and route-table formatting. A dedicated ASA route parser keeps the IOS parser away from ASA output (it corrupts it silently); the interface parser is reused because it was measured to handle ASA output correctly. The command allowlist gains three ungated read-only entries, monitoring picks its base commands by `device_type`, and the system prompt points the model at the ASA vocabulary.

**Tech Stack:** Python 3.13, `ipaddress` (standard library), pytest — no new dependency.

**Spec:** `docs/superpowers/specs/2026-08-03-asa-read-support-design.md`

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-03-asa-read-support-design.md` — this plan implements it in full.
- **Read paths only.** ASA *configuration* stays Preview-only/out of scope, exactly as `backend/README.md` already states. No task here touches the change/batch workflow.
- **The policy engine must not learn about `device_type`.** It answers "is this command read-only and safe", which is device-independent. Syntax correctness belongs to the context and prompt. This keeps the change out of the safety-critical policy logic.
- Test fixtures use the real FW-01 output captured on 2026-08-03, reproduced verbatim in Task 1 — not invented samples.
- No migration, no new dependency, no frontend change.
- Use the project's Python 3.13 venv (`../.venv/Scripts/python.exe` from `backend/`) for every command.

---

### Task 1: ASA route parser

**Files:**
- Create: `backend/src/network_copilot/parsers/asa_routes.py`
- Modify: `backend/src/network_copilot/parsers/__init__.py`
- Test: `backend/tests/parsers/test_asa_routes.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `parse_asa_routes(raw: str | None) -> list[dict]`, each dict `{network, protocol, next_hop, interface, distance, metric}` — the same shape `parse_ip_routes` already returns, so every existing consumer (chat result tables, monitoring snapshots) works unchanged. Registered in `PARSERS` under `"show route"`; `"show interface ip brief"` is registered to the existing `parse_ip_interface_brief`. Task 3 relies on both registrations.

- [x] **Step 1: Write the failing tests**

Create `backend/tests/parsers/test_asa_routes.py`:

```python
from network_copilot.parsers import parse_command_output
from network_copilot.parsers.asa_routes import parse_asa_routes

# Captured verbatim from FW-01 (Cisco ASA 9.5(2)204) on 2026-08-03.
ASA_ROUTES = """Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2
       E1 - OSPF external type 1, E2 - OSPF external type 2
       i - IS-IS, su - IS-IS summary, L1 - IS-IS level-1, L2 - IS-IS level-2
       ia - IS-IS inter area, * - candidate default, U - per-user static route
       o - ODR, P - periodic downloaded static route, + - replicated route
Gateway of last resort is 10.255.0.1 to network 0.0.0.0

S*       0.0.0.0 0.0.0.0 [1/0] via 10.255.0.1, OUTSIDE
C        10.10.10.0 255.255.255.0 is directly connected, MGMT
L        10.10.10.3 255.255.255.255 is directly connected, MGMT
C        10.10.100.0 255.255.255.0 is directly connected, DMZ
L        10.10.100.1 255.255.255.255 is directly connected, DMZ
C        10.255.0.0 255.255.255.252 is directly connected, OUTSIDE
L        10.255.0.2 255.255.255.255 is directly connected, OUTSIDE
C        10.255.0.4 255.255.255.252 is directly connected, INSIDE
L        10.255.0.5 255.255.255.255 is directly connected, INSIDE
"""


def _by_network(rows, network):
    return next(row for row in rows if row["network"] == network)


def test_netmask_becomes_a_prefix_length():
    """ASA prints "255.255.255.0" where IOS prints "/24". The IOS parser
    reads that as trailing prose and falls back to /32."""
    rows = parse_asa_routes(ASA_ROUTES)
    row = _by_network(rows, "10.10.10.0/24")
    assert row["protocol"] == "C"
    assert row["interface"] == "MGMT"


def test_a_thirty_bit_mask_is_converted():
    rows = parse_asa_routes(ASA_ROUTES)
    assert _by_network(rows, "10.255.0.4/30")["interface"] == "INSIDE"


def test_default_route_is_not_a_host_route():
    """"0.0.0.0 0.0.0.0" must become 0.0.0.0/0, not 0.0.0.0/32."""
    rows = parse_asa_routes(ASA_ROUTES)
    row = _by_network(rows, "0.0.0.0/0")
    assert row["protocol"] == "S"
    assert row["next_hop"] == "10.255.0.1"
    assert row["distance"] == 1
    assert row["metric"] == 0
    assert row["interface"] == "OUTSIDE"


def test_local_host_routes_are_parsed_not_dropped():
    rows = parse_asa_routes(ASA_ROUTES)
    row = _by_network(rows, "10.10.10.3/32")
    assert row["protocol"] == "L"


def test_nameif_interfaces_are_kept_verbatim():
    """ASA route lines end in a nameif (MGMT, DMZ, INSIDE, OUTSIDE), not a
    physical interface name. The IOS interface pattern requires a digit and
    would drop all of these."""
    rows = parse_asa_routes(ASA_ROUTES)
    names = {row["interface"] for row in rows}
    assert {"MGMT", "DMZ", "INSIDE", "OUTSIDE"} <= names


def test_legend_and_gateway_lines_produce_no_rows():
    rows = parse_asa_routes(ASA_ROUTES)
    assert len(rows) == 9


def test_empty_input_returns_an_empty_list():
    assert parse_asa_routes("") == []
    assert parse_asa_routes(None) == []


def test_show_route_is_routed_to_the_asa_parser():
    rows = parse_command_output("show route", ASA_ROUTES)
    assert _by_network(rows, "10.10.100.0/24")["interface"] == "DMZ"


def test_show_ip_route_still_uses_the_ios_parser():
    """The two parsers must never see each other's output again."""
    ios = "C        10.10.10.0/24 is directly connected, GigabitEthernet0/1\n"
    rows = parse_command_output("show ip route", ios)
    assert rows[0]["network"] == "10.10.10.0/24"
    assert rows[0]["interface"] == "GigabitEthernet0/1"
```

Also add, in the same file, the test that pins the interface-parser reuse decision:

```python
from network_copilot.parsers import parse_ip_interface_brief

# Captured verbatim from FW-01 on 2026-08-03. Identical in shape to IOS.
ASA_INTERFACES = """Interface                  IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0         10.255.0.2      YES CONFIG up                    up
GigabitEthernet0/1         10.10.100.1     YES CONFIG up                    up
GigabitEthernet0/4         unassigned      YES unset  administratively down down
"""


def test_ios_interface_parser_handles_asa_output():
    """The reuse decision is measured, not assumed: this pins it so a future
    edit to the IOS parser cannot silently break ASA."""
    rows = parse_ip_interface_brief(ASA_INTERFACES)
    assert rows[0] == {
        "interface": "GigabitEthernet0/0",
        "ip_address": "10.255.0.2",
        "status": "up",
        "protocol": "up",
    }
    assert rows[2]["ip_address"] == "unassigned"
    assert rows[2]["status"] == "administratively down"


def test_asa_interface_command_is_routed_to_the_shared_parser():
    rows = parse_command_output("show interface ip brief", ASA_INTERFACES)
    assert rows[1]["ip_address"] == "10.10.100.1"
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/parsers/test_asa_routes.py -v` (from `backend/`)
Expected: FAIL at import — `ModuleNotFoundError: No module named 'network_copilot.parsers.asa_routes'`.

- [x] **Step 3: Write the parser**

Create `backend/src/network_copilot/parsers/asa_routes.py`:

```python
"""Parser for the Cisco ASA `show route` command.

ASA prints a netmask where IOS prints a prefix length, and the interface's
`nameif` (OUTSIDE, INSIDE, DMZ, MGMT) where IOS prints a physical
interface name. Measured against real FW-01 output, feeding ASA text to
parsers/routes.py silently yields /32 networks and a null interface - no
exception, just wrong data - so the two never share a parser.
"""

import ipaddress
import re

# "C        10.10.10.0 255.255.255.0 is directly connected, MGMT"
# "S*       0.0.0.0 0.0.0.0 [1/0] via 10.255.0.1, OUTSIDE"
ROUTE = re.compile(
    r"^(?P<protocol>[A-Za-z]{1,2}\*?"
    r"(?:\s+(?:IA|EX|E1|E2|N1|N2|L1|L2))?)\s+"
    r"(?P<network>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<netmask>\d{1,3}(?:\.\d{1,3}){3})\s*"
    r"(?P<rest>.*)$"
)

METRIC = re.compile(r"\[(?P<distance>\d+)/(?P<metric>\d+)\]")
VIA = re.compile(r"via\s+(?P<next_hop>\d{1,3}(?:\.\d{1,3}){3})")

SKIP_PREFIXES = ("Codes:", "Gateway of last resort")


def parse_asa_routes(raw: str | None) -> list[dict]:
    if not raw:
        return []

    rows: list[dict] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(SKIP_PREFIXES):
            continue

        match = ROUTE.match(stripped)
        if match is None:
            # Legend continuation lines carry no address pair and fall out
            # here, the same way the IOS parser ignores them.
            continue

        try:
            network = ipaddress.IPv4Network(
                f"{match.group('network')}/{match.group('netmask')}", strict=False
            )
        except ValueError:  # pragma: no cover - defensive against odd output
            continue

        rest = match.group("rest")
        metric = METRIC.search(rest)
        via = VIA.search(rest)

        interface = None
        if "," in rest:
            tail = rest.rsplit(",", 1)[1].strip()
            if tail:
                interface = tail

        rows.append(
            {
                "network": str(network),
                "protocol": re.sub(r"\s+", " ", match.group("protocol"))
                .replace("*", "")
                .strip(),
                "next_hop": via.group("next_hop") if via else None,
                "interface": interface,
                "distance": int(metric.group("distance")) if metric else None,
                "metric": int(metric.group("metric")) if metric else None,
            }
        )

    return rows
```

- [x] **Step 4: Register both ASA commands**

Replace the whole of `backend/src/network_copilot/parsers/__init__.py`:

```python
from .asa_routes import parse_asa_routes
from .interfaces import parse_ip_interface_brief
from .ospf import parse_ospf_neighbors
from .routes import parse_ip_routes
from .vlans import parse_vlan_brief

# Maps a normalised command to the parser that understands its output.
# "show interface ip brief" is the ASA spelling; its output format was
# measured to be identical to IOS, so it shares the IOS parser. "show route"
# is also ASA but needs its own parser - see parsers/asa_routes.py.
PARSERS = {
    "show ip interface brief": parse_ip_interface_brief,
    "show interface ip brief": parse_ip_interface_brief,
    "show vlan brief": parse_vlan_brief,
    "show ip route": parse_ip_routes,
    "show route": parse_asa_routes,
    "show ip ospf neighbor": parse_ospf_neighbors,
}


def parse_command_output(command: str, raw: str) -> list[dict] | None:
    """Parse known command output. Returns None when no parser is registered.

    Callers must always persist the raw output as well: a parser returning an
    empty list is not proof that the device reported nothing.
    """
    parser = PARSERS.get(command)
    if parser is None:
        return None
    try:
        return parser(raw)
    except Exception:  # pragma: no cover - a parser must never break a poll
        return []


__all__ = [
    "PARSERS",
    "parse_asa_routes",
    "parse_command_output",
    "parse_ip_interface_brief",
    "parse_ip_routes",
    "parse_ospf_neighbors",
    "parse_vlan_brief",
]
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/parsers/ -v`
Expected: PASS — the 11 new tests, plus every pre-existing parser test unchanged (the IOS parsers were not modified).

- [x] **Step 6: Commit**

```bash
git add backend/src/network_copilot/parsers/asa_routes.py backend/src/network_copilot/parsers/__init__.py backend/tests/parsers/test_asa_routes.py
git commit -m "feat: parse Cisco ASA route and interface output"
```

---

### Task 2: Allow the three ASA read commands

**Files:**
- Modify: `backend/src/network_copilot/commands/policy.py` (add three entries to `READ_ONLY_RULES`)
- Test: `backend/tests/commands/test_policy.py` (append)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `default_policy.evaluate()` returning `allowed=True` for `show interface ip brief`, `show route` and `show access-list` on any role. Task 3 relies on the first two being allowed, since monitoring runs them.

- [x] **Step 1: Write the failing test**

Append to `backend/tests/commands/test_policy.py`:

```python
@pytest.mark.parametrize(
    "command",
    ["show interface ip brief", "show route", "show access-list"],
)
@pytest.mark.parametrize("role", ["firewall", "core", "access"])
def test_asa_read_commands_are_allowed_on_any_role(command, role):
    """The policy engine answers "is this read-only and safe", which does not
    depend on the device type. Whether a device understands the syntax is a
    correctness question handled by the AI context and prompt, so these rules
    are deliberately ungated."""
    decision = default_policy.evaluate(command, role)
    assert decision.allowed is True
```

No import change is needed: `tests/commands/test_policy.py` already imports
`pytest` and `default_policy` at the top of the file.

- [x] **Step 2: Run the test to verify it fails**

Run: `../.venv/Scripts/python.exe -m pytest tests/commands/test_policy.py -v -k asa_read_commands` (from `backend/`)
Expected: FAIL — `assert False is True` for all 9 parameter combinations, because the allowlist denies anything it does not explicitly recognise.

- [x] **Step 3: Add the rules**

In `backend/src/network_copilot/commands/policy.py`, find this entry inside `READ_ONLY_RULES`:

```python
    CommandRule(
        "show ip route", _exact("show ip route"), description="IPv4 routing table"
    ),
```

Insert the three ASA rules immediately after it:

```python
    CommandRule(
        "show ip route", _exact("show ip route"), description="IPv4 routing table"
    ),
    # Cisco ASA spellings. Deliberately ungated by role or device type: the
    # policy engine's question is "is this read-only and safe", which is the
    # same answer everywhere. An IOS device asked for "show route" simply
    # returns its own syntax error, which is the symmetric case of what an
    # ASA does with IOS syntax today and is no worse.
    CommandRule(
        "show interface ip brief",
        _exact("show interface ip brief"),
        description="ASA interface addressing and line state",
    ),
    CommandRule(
        "show route", _exact("show route"), description="ASA IPv4 routing table"
    ),
    CommandRule(
        "show access-list",
        _exact("show access-list"),
        description="ASA ACL definitions",
    ),
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/commands/test_policy.py -v`
Expected: PASS — the 9 new parameter combinations, plus every pre-existing policy test unchanged.

- [x] **Step 5: Commit**

```bash
git add backend/src/network_copilot/commands/policy.py backend/tests/commands/test_policy.py
git commit -m "feat: allow the ASA read-only command spellings"
```

---

### Task 3: Monitoring picks base commands by device type

**Files:**
- Modify: `backend/src/network_copilot/monitoring/service.py:16-31` (the command-selection block) and `:66` (the `poll_device` loop)
- Test: `backend/tests/monitoring/test_monitoring.py` (append)

**Interfaces:**
- Consumes: the parser registrations from Task 1 (so ASA poll output becomes structured data) and the allowlist entries from Task 2.
- Produces: `commands_for_device(device) -> list[str]`, choosing the ASA base commands when `device.device_type == "cisco_asa"`. `commands_for_role(role) -> list[str]` keeps its exact current signature and output, so the six existing tests that call it stay valid.

- [x] **Step 1: Write the failing tests**

Append to `backend/tests/monitoring/test_monitoring.py`:

```python
def test_asa_devices_poll_the_asa_command_spellings(app, make_device):
    from network_copilot.monitoring.service import commands_for_device

    firewall = make_device("FW-TEST", "10.0.0.99", "firewall", device_type="cisco_asa")

    assert commands_for_device(firewall) == [
        "show interface ip brief",
        "show route",
    ]


def test_ios_devices_are_unaffected_by_the_asa_branch(app, make_device):
    from network_copilot.monitoring.service import commands_for_device

    switch = make_device("DIST-TEST", "10.0.0.98", "distribution")

    assert commands_for_device(switch) == [
        "show ip interface brief",
        "show ip route",
        "show ip ospf neighbor",
        "show vlan brief",
        "show interfaces trunk",
        "show ip dhcp pool",
    ]


def test_asa_gets_no_role_extras(app, make_device):
    """"firewall" is in neither ROUTING_ROLES nor SWITCHING_ROLES, so an ASA
    is never asked for OSPF neighbours or a VLAN database it does not have."""
    from network_copilot.monitoring.service import commands_for_device

    firewall = make_device("FW-TEST2", "10.0.0.97", "firewall", device_type="cisco_asa")

    assert "show vlan brief" not in commands_for_device(firewall)
    assert "show ip ospf neighbor" not in commands_for_device(firewall)


def test_poll_runs_the_asa_commands_on_an_asa_device(app, ssh_factory, make_device):
    from network_copilot.monitoring.service import poll_device

    firewall = make_device("FW-TEST3", "10.0.0.96", "firewall", device_type="cisco_asa")
    fake = ssh_factory.set_client(firewall.hostname, default_output="ok")

    poll_device(firewall.id)

    assert fake.show_commands == ["show interface ip brief", "show route"]
```

The `make_device` fixture in `tests/conftest.py` already accepts `device_type` as its fourth parameter, defaulting to `"cisco_ios"`.

- [x] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/monitoring/test_monitoring.py -v -k "asa or unaffected"` (from `backend/`)
Expected: FAIL — `ImportError: cannot import name 'commands_for_device'` for all four.

- [x] **Step 3: Split base commands and add the device-aware selector**

In `backend/src/network_copilot/monitoring/service.py`, find:

```python
BASE_COMMANDS = ["show ip interface brief", "show ip route"]
ROUTING_ROLES = {"core", "distribution"}
SWITCHING_ROLES = {"access", "distribution"}


def commands_for_role(role: str) -> list[str]:
    """Read-only commands to poll for a device in the given role."""
    commands = list(BASE_COMMANDS)
    if role in ROUTING_ROLES:
        commands.append("show ip ospf neighbor")
    if role in SWITCHING_ROLES:
        commands.append("show vlan brief")
        commands.append("show interfaces trunk")
    if role in ROUTING_ROLES:
        commands.append("show ip dhcp pool")
    return commands
```

Replace with:

```python
IOS_BASE_COMMANDS = ["show ip interface brief", "show ip route"]
# Cisco ASA uses a different vocabulary for the same two questions.
ASA_BASE_COMMANDS = ["show interface ip brief", "show route"]
ASA_DEVICE_TYPES = {"cisco_asa"}

# Kept as the IOS base so existing callers and tests are unaffected.
BASE_COMMANDS = IOS_BASE_COMMANDS

ROUTING_ROLES = {"core", "distribution"}
SWITCHING_ROLES = {"access", "distribution"}


def _role_extras(role: str) -> list[str]:
    """Role-driven additions, identical for every device type."""
    extras: list[str] = []
    if role in ROUTING_ROLES:
        extras.append("show ip ospf neighbor")
    if role in SWITCHING_ROLES:
        extras.append("show vlan brief")
        extras.append("show interfaces trunk")
    if role in ROUTING_ROLES:
        extras.append("show ip dhcp pool")
    return extras


def commands_for_role(role: str) -> list[str]:
    """Read-only IOS commands to poll for a device in the given role."""
    return list(IOS_BASE_COMMANDS) + _role_extras(role)


def commands_for_device(device: Device) -> list[str]:
    """Read-only commands to poll for one device, honouring its type.

    This is the one place that genuinely needs device_type: it chooses
    commands with no model in the loop, so nothing else can catch a
    wrong-vendor spelling before it reaches the device.
    """
    base = (
        ASA_BASE_COMMANDS
        if device.device_type in ASA_DEVICE_TYPES
        else IOS_BASE_COMMANDS
    )
    return list(base) + _role_extras(device.role)
```

Then find, inside `poll_device`:

```python
        for command in commands_for_role(device.role):
```

Replace with:

```python
        for command in commands_for_device(device):
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/monitoring/test_monitoring.py -v`
Expected: PASS — the 4 new tests, plus the 6 pre-existing `commands_for_role` tests unchanged (the refactor preserves its exact output and ordering).

- [x] **Step 5: Commit**

```bash
git add backend/src/network_copilot/monitoring/service.py backend/tests/monitoring/test_monitoring.py
git commit -m "feat: poll ASA devices with ASA command spellings"
```

---

### Task 4: Teach the model the ASA vocabulary

**Files:**
- Modify: `backend/src/network_copilot/ai/service.py` (`SYSTEM_PROMPT` and `build_context`)
- Test: `backend/tests/ai/test_ai.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks at runtime — this task is independent of them, though the feature only works end to end once all four land.
- Produces: `build_context()` returning an additional `"asa_command_equivalents"` key (a `dict[str, str]` mapping IOS spelling to ASA spelling). Nothing later depends on it — this is the last task.

- [x] **Step 1: Write the failing tests**

Append to `backend/tests/ai/test_ai.py`:

```python
def test_context_carries_the_asa_command_equivalents(app, admin_user):
    service, provider = service_with(app, MONITOR_ACTION)
    service.interpret("Kiem tra OSPF cua DIST-SW1", admin_user.id)

    equivalents = provider.prompts[0]["context"]["asa_command_equivalents"]
    assert equivalents["show ip interface brief"] == "show interface ip brief"
    assert equivalents["show ip route"] == "show route"
    assert equivalents["show access-lists"] == "show access-list"


def test_prompt_tells_the_model_to_use_asa_syntax_on_asa_devices(app, admin_user):
    """FW-01 answered "kiem tra ket noi fw01" with
    "ERROR: % Invalid input detected" because the model sent IOS syntax to an
    ASA. The rule must stay in the prompt or that returns."""
    service, provider = service_with(app, MONITOR_ACTION)
    service.interpret("Kiem tra OSPF cua DIST-SW1", admin_user.id)

    prompt = provider.prompts[0]["system_prompt"]
    assert "cisco_asa" in prompt
    assert "asa_command_equivalents" in prompt
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "asa"` (from `backend/`)
Expected: FAIL — `KeyError: 'asa_command_equivalents'` on the first, and `assert 'cisco_asa' in prompt` on the second.

- [x] **Step 3: Add the context mapping**

In `backend/src/network_copilot/ai/service.py`, find the constant block near the top:

```python
CONTEXT_EXCLUDED_COMMANDS = {"show running-config"}
```

Add the mapping immediately after it:

```python
CONTEXT_EXCLUDED_COMMANDS = {"show running-config"}

# Cisco ASA answers the same questions with different words. The model is
# told each device's device_type already; this is the vocabulary it needs to
# act on that. Measured against FW-01 (ASA 9.5): sending the IOS spelling
# returns "ERROR: % Invalid input detected".
ASA_COMMAND_EQUIVALENTS = {
    "show ip interface brief": "show interface ip brief",
    "show ip route": "show route",
    "show access-lists": "show access-list",
}
```

Then find the end of `build_context()`:

```python
        return {
            "devices": [
                {
                    "hostname": device.hostname,
                    "role": device.role,
                    "device_type": device.device_type,
                    "status": device.status,
                }
                for device in devices
            ],
            "supported_commands": commands,
        }
```

Replace with:

```python
        return {
            "devices": [
                {
                    "hostname": device.hostname,
                    "role": device.role,
                    "device_type": device.device_type,
                    "status": device.status,
                }
                for device in devices
            ],
            "supported_commands": commands,
            "asa_command_equivalents": ASA_COMMAND_EQUIVALENTS,
        }
```

- [x] **Step 4: Add the prompt rule**

In the same file, find this rule inside `SYSTEM_PROMPT`:

```
- For "monitor" and "troubleshoot", return exactly one operation for one explicit
  hostname in "exec" mode. Use only read-only entries from supported_commands.
```

Replace with:

```
- For "monitor" and "troubleshoot", return exactly one operation for one explicit
  hostname in "exec" mode. Use only read-only entries from supported_commands.
- A device whose "device_type" is "cisco_asa" does not understand IOS syntax.
  For those devices, replace the IOS command with its ASA spelling from
  "asa_command_equivalents" in the context. Sending the IOS form to an ASA
  returns a syntax error, not data.
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "asa"`
Expected: PASS (2 tests)

- [x] **Step 6: Run the full backend test suite**

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass — 698 as of this plan plus the tests added across Tasks 1-4; none should fail. This specifically confirms that adding a context key and three allowlist entries broke no existing AI, policy, monitoring or E2E test.

- [x] **Step 7: Commit**

```bash
git add backend/src/network_copilot/ai/service.py backend/tests/ai/test_ai.py
git commit -m "feat: point the AI at ASA command spellings for ASA devices"
```

---

### Task 5: Live-lab verification

**Files:** none — verification only. If a defect is found, fix it in the file it belongs to and note that in the commit.

**Interfaces:** none — this exercises Tasks 1-4 together against the real FW-01.

- [ ] **Step 1: Deploy**

On the AI Server:

```bash
git pull origin main
```

Restart the Flask process.

- [ ] **Step 2: Confirm the original failure is gone**

In the chat page, send the request that motivated this work:

```
kiem tra ket noi fw01
```

Expected: a real interface table for FW-01 — `GigabitEthernet0/0` at `10.255.0.2`, `GigabitEthernet0/1` at `10.10.100.1`, `GigabitEthernet0/3` at `10.10.10.3` — rendered as a table, **not** `ERROR: % Invalid input detected`. This is the acceptance criterion for the whole feature.

- [ ] **Step 3: Confirm the ASA routing table parses**

```
Kiem tra bang dinh tuyen tren FW-01
```

Expected: a table whose NETWORK column shows real prefixes (`10.10.10.0/24`, `10.10.100.0/24`, `10.255.0.4/30`, `0.0.0.0/0`) and whose INTERFACE column shows nameifs (`MGMT`, `DMZ`, `INSIDE`, `OUTSIDE`). Seeing `/32` on everything would mean the IOS parser is still being used.

- [ ] **Step 4: Confirm IOS devices are unchanged**

```
Kiem tra bang dinh tuyen tren INTERNAL-RTR
```

Expected: exactly the same output as before this change — physical interface names, OSPF routes with metric 2. This is the regression check that matters most, since eight of the nine devices are IOS.

- [ ] **Step 5: Confirm monitoring stores real data for FW-01**

With `MONITORING_ENABLED=true`, wait one poll interval, then on the AI Server:

```bash
.venv/bin/python -c "
from network_copilot.app import create_app
from network_copilot.devices.service import get_device_by_hostname
from network_copilot.monitoring.service import latest_snapshot
app = create_app()
with app.app_context():
    fw = get_device_by_hostname('FW-01')
    snap = latest_snapshot(fw.id)
    print(sorted(snap.parsed_data)) if snap else print('no snapshot yet')
"
```

Expected: `['show interface ip brief', 'show route']` — proof that the poll ran the ASA spellings and both produced structured data.

- [ ] **Step 6: Report**

If everything above holds, the feature is done. Note any defect found and fixed in a follow-up commit (`fix: <description>`); if nothing changed, no commit is needed for this task.
