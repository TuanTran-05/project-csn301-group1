# AI Network Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the model a real map of the network — which VLAN carries which subnet, where its gateway lives, and which router interface reaches it — so a plain request like *"chặn guest ping tới IT"* resolves to real subnets and a correct interface instead of the `deny ip any any` that Batch #17 produced.

**Architecture:** A new `ai/topology.py` joins three already-parsed monitoring snapshot commands (`show ip interface brief`, `show vlan brief`, `show ip route`) into a VLAN↔subnet↔gateway map plus per-router routes, filters out any subnet containing a device's management IP, and `build_context()` publishes it under a `"topology"` key. Four ACL domain rules are added to the system prompt.

**Tech Stack:** Python 3.13, `ipaddress` (standard library), pytest — no new dependency, no migration, no frontend change.

**Spec:** `docs/superpowers/specs/2026-08-03-ai-network-context-design.md`

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-03-ai-network-context-design.md` — this plan implements it in full.
- **The management-IP filter is data-driven, never hardcoded.** It drops any network containing a `Device.management_ip` from the inventory. No IP literal appears in the source.
- **Fail closed.** A subnet string that cannot be parsed as a network is dropped, not passed through. A malformed value must never become a hole in the filter.
- The narrowed security rule: the model never receives **credentials**, **management IPs**, or a **full running-config**; it *does* receive user network subnets and gateways. `ai/service.py`'s module docstring is updated in the same change so the stated rule and the code cannot drift.
- No change to the policy engine, the change/batch workflow, or the frontend.
- Degradation is a requirement, not an afterthought: with no snapshots (`MONITORING_ENABLED=false`, which is what `backend/.env` sets locally) the maps come back empty and the model behaves exactly as it does today.
- Use the project's Python 3.13 venv (`../.venv/Scripts/python.exe` from `backend/`) for every command.

---

### Task 1: `ai/topology.py` — the VLAN↔subnet↔gateway join, with the management filter

**Files:**
- Create: `backend/src/network_copilot/ai/topology.py`
- Test: `backend/tests/ai/test_topology.py`

**Interfaces:**
- Consumes: `devices.service.list_devices() -> list[Device]` (ordered by hostname; each has `.id`, `.hostname`, `.role`, `.management_ip`); `monitoring.service.latest_snapshot(device_id: int) -> DeviceSnapshot | None` (has `.parsed_data: dict` keyed by command string). Parsed shapes: `show ip interface brief` → `{interface, ip_address, status, protocol}`; `show vlan brief` → `{vlan_id, name, status, ports}`; `show ip route` → `{network, protocol, next_hop, interface, distance, metric}`.
- Produces: `build_topology() -> dict` with a `"networks"` list (this task) and a `"routing"` list (added in Task 2, empty here). Each `networks` entry: `{vlan_id: int, subnet: str, gateway: str, gateway_device: str, gateway_interface: str}` plus an optional `name: str`. Task 3 publishes the whole dict into `build_context()`.

The filter ships in this task, not a later one, so no intermediate commit ever exposes a management network.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/ai/test_topology.py`:

```python
from network_copilot.ai.topology import build_topology
from network_copilot.extensions import db
from network_copilot.monitoring.model import DeviceSnapshot


def _snapshot(device, parsed_data):
    snapshot = DeviceSnapshot(
        device_id=device.id,
        status="online",
        raw_output={},
        parsed_data=parsed_data,
    )
    db.session.add(snapshot)
    db.session.commit()
    return snapshot


def _dist_snapshot(device, vlan_id=60, name="GUEST", gateway="10.10.60.1",
                   subnet="10.10.60.0/24"):
    return _snapshot(
        device,
        {
            "show ip interface brief": [
                {
                    "interface": f"Vlan{vlan_id}",
                    "ip_address": gateway,
                    "status": "up",
                    "protocol": "up",
                }
            ],
            "show vlan brief": [
                {"vlan_id": vlan_id, "name": name, "status": "active", "ports": []}
            ],
            "show ip route": [
                {
                    "network": subnet,
                    "protocol": "C",
                    "next_hop": None,
                    "interface": f"Vlan{vlan_id}",
                    "distance": None,
                    "metric": None,
                }
            ],
        },
    )


def test_joins_vlan_name_subnet_and_gateway(app, make_device):
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _dist_snapshot(switch)

    networks = build_topology()["networks"]

    assert networks == [
        {
            "vlan_id": 60,
            "name": "GUEST",
            "subnet": "10.10.60.0/24",
            "gateway": "10.10.60.1",
            "gateway_device": "DIST-SW2",
            "gateway_interface": "Vlan60",
        }
    ]


def test_an_svi_without_an_address_is_skipped(app, make_device):
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _snapshot(
        switch,
        {
            "show ip interface brief": [
                {
                    "interface": "Vlan10",
                    "ip_address": "unassigned",
                    "status": "down",
                    "protocol": "down",
                }
            ],
            "show vlan brief": [
                {"vlan_id": 10, "name": "MGMT", "status": "active", "ports": []}
            ],
            "show ip route": [],
        },
    )

    assert build_topology()["networks"] == []


def test_an_svi_without_a_connected_route_is_skipped(app, make_device):
    """No route means no prefix length, and a half-populated entry would
    mislead the model about the size of the network."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _snapshot(
        switch,
        {
            "show ip interface brief": [
                {
                    "interface": "Vlan60",
                    "ip_address": "10.10.60.1",
                    "status": "up",
                    "protocol": "up",
                }
            ],
            "show vlan brief": [
                {"vlan_id": 60, "name": "GUEST", "status": "active", "ports": []}
            ],
            "show ip route": [],
        },
    )

    assert build_topology()["networks"] == []


def test_local_host_routes_are_not_used_as_subnets(app, make_device):
    """A /32 "L" route must never become the subnet: that would make the
    network look 1 address wide and defeat the management filter."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _snapshot(
        switch,
        {
            "show ip interface brief": [
                {
                    "interface": "Vlan60",
                    "ip_address": "10.10.60.1",
                    "status": "up",
                    "protocol": "up",
                }
            ],
            "show vlan brief": [],
            "show ip route": [
                {
                    "network": "10.10.60.1/32",
                    "protocol": "L",
                    "next_hop": None,
                    "interface": "Vlan60",
                    "distance": None,
                    "metric": None,
                }
            ],
        },
    )

    assert build_topology()["networks"] == []


def test_a_device_without_vlan_data_still_yields_an_entry(app, make_device):
    """A core router is not polled for VLANs. The entry keeps its subnet and
    gateway and simply has no name, rather than being dropped."""
    router = make_device("INTERNAL-RTR", "10.10.10.11", "core")
    _snapshot(
        router,
        {
            "show ip interface brief": [
                {
                    "interface": "Vlan99",
                    "ip_address": "10.10.99.1",
                    "status": "up",
                    "protocol": "up",
                }
            ],
            "show ip route": [
                {
                    "network": "10.10.99.0/24",
                    "protocol": "C",
                    "next_hop": None,
                    "interface": "Vlan99",
                    "distance": None,
                    "metric": None,
                }
            ],
        },
    )

    entry = build_topology()["networks"][0]
    assert entry["subnet"] == "10.10.99.0/24"
    assert "name" not in entry


def test_a_network_holding_a_management_ip_is_dropped(app, make_device):
    """THE security test. 10.10.10.22 is DIST-SW2's own management address,
    so 10.10.10.0/24 must never reach the model."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _snapshot(
        switch,
        {
            "show ip interface brief": [
                {
                    "interface": "Vlan10",
                    "ip_address": "10.10.10.22",
                    "status": "up",
                    "protocol": "up",
                },
                {
                    "interface": "Vlan60",
                    "ip_address": "10.10.60.1",
                    "status": "up",
                    "protocol": "up",
                },
            ],
            "show vlan brief": [
                {"vlan_id": 10, "name": "MGMT", "status": "active", "ports": []},
                {"vlan_id": 60, "name": "GUEST", "status": "active", "ports": []},
            ],
            "show ip route": [
                {
                    "network": "10.10.10.0/24",
                    "protocol": "C",
                    "next_hop": None,
                    "interface": "Vlan10",
                    "distance": None,
                    "metric": None,
                },
                {
                    "network": "10.10.60.0/24",
                    "protocol": "C",
                    "next_hop": None,
                    "interface": "Vlan60",
                    "distance": None,
                    "metric": None,
                },
            ],
        },
    )

    subnets = [entry["subnet"] for entry in build_topology()["networks"]]
    assert subnets == ["10.10.60.0/24"]


def test_another_devices_management_ip_also_filters(app, make_device):
    """The filter uses the whole inventory, not just the device that owns the
    SVI, so one switch cannot expose another's management range."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    make_device("ACC-SW1", "10.10.10.31", "access")
    _dist_snapshot(switch, vlan_id=10, name="MGMT", gateway="10.10.10.1",
                   subnet="10.10.10.0/24")

    assert build_topology()["networks"] == []


def test_an_unparseable_subnet_is_dropped(app, make_device):
    """Fail closed: a value the filter cannot evaluate must not slip past it."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _dist_snapshot(switch, subnet="not-a-network")

    assert build_topology()["networks"] == []


def test_networks_are_sorted_by_vlan_then_device(app, make_device):
    second = make_device("DIST-SW2", "10.10.10.22", "distribution")
    first = make_device("DIST-SW1", "10.10.10.21", "distribution")
    _dist_snapshot(second, vlan_id=60, name="GUEST", gateway="10.10.60.1",
                   subnet="10.10.60.0/24")
    _dist_snapshot(first, vlan_id=20, name="HR", gateway="10.10.20.1",
                   subnet="10.10.20.0/24")

    networks = build_topology()["networks"]
    assert [entry["vlan_id"] for entry in networks] == [20, 60]


def test_no_snapshots_yields_empty_maps(app, make_device):
    """MONITORING_ENABLED is false by default. The feature must be inert,
    not broken, on a deployment with no snapshots."""
    make_device("DIST-SW2", "10.10.10.22", "distribution")

    topology = build_topology()

    assert topology == {"networks": [], "routing": []}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_topology.py -v` (from `backend/`)
Expected: FAIL at import — `ModuleNotFoundError: No module named 'network_copilot.ai.topology'`.

- [ ] **Step 3: Write the module**

Create `backend/src/network_copilot/ai/topology.py`:

```python
"""Network topology derived from monitoring snapshots.

Gives the model enough of the real network to reason about policy - which
VLAN carries which subnet, where its gateway lives, and which interface a
router uses to reach it - without ever handing over a management IP.

Everything here is read from data the monitoring scheduler already
collects and parses; nothing opens an SSH session, so building the map
costs a chat turn nothing.
"""

import ipaddress
import re

from ..devices import service as device_service
from ..monitoring.service import latest_snapshot

INTERFACE_COMMAND = "show ip interface brief"
VLAN_COMMAND = "show vlan brief"
ROUTE_COMMAND = "show ip route"

# Only these roles hold a routing table worth showing the model.
ROUTING_ROLES = {"core", "distribution"}

# A switched virtual interface: "Vlan60" carries VLAN 60.
_SVI = re.compile(r"^Vlan(?P<vlan_id>\d+)$", re.I)

# Cisco's protocol letter for a directly connected network. "L" is the
# local /32 host route and is deliberately not accepted: it would make a
# /24 look one address wide and defeat the management filter below.
_CONNECTED = "C"


def _parsed(snapshot, command: str) -> list[dict]:
    """Rows for one command in a snapshot, tolerating anything malformed."""
    if snapshot is None or not isinstance(snapshot.parsed_data, dict):
        return []
    rows = snapshot.parsed_data.get(command)
    return rows if isinstance(rows, list) else []


def _management_ips(devices) -> list:
    addresses = []
    for device in devices:
        if not device.management_ip:
            continue
        try:
            addresses.append(ipaddress.ip_address(device.management_ip))
        except ValueError:
            continue
    return addresses


def _hides_a_management_ip(subnet: str, management_ips) -> bool:
    """True when this subnet must not be shown to the model.

    Fails closed: a subnet that cannot be parsed is treated as unsafe
    rather than waved through, so a malformed value can never become a
    hole in the filter.
    """
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return True
    return any(address in network for address in management_ips)


def _networks_for(device, snapshot) -> list[dict]:
    vlan_names = {
        row["vlan_id"]: row.get("name")
        for row in _parsed(snapshot, VLAN_COMMAND)
        if isinstance(row.get("vlan_id"), int)
    }
    connected = {
        row["interface"]: row["network"]
        for row in _parsed(snapshot, ROUTE_COMMAND)
        if row.get("protocol") == _CONNECTED and row.get("interface")
    }

    entries = []
    for row in _parsed(snapshot, INTERFACE_COMMAND):
        name = row.get("interface") or ""
        match = _SVI.match(name)
        if match is None:
            continue

        address = row.get("ip_address")
        if not address or address == "unassigned":
            continue

        subnet = connected.get(name)
        if subnet is None:
            # No connected route means no prefix length. A half-populated
            # entry would misstate the size of the network.
            continue

        vlan_id = int(match.group("vlan_id"))
        entry = {
            "vlan_id": vlan_id,
            "subnet": subnet,
            "gateway": address,
            "gateway_device": device.hostname,
            "gateway_interface": name,
        }
        vlan_name = vlan_names.get(vlan_id)
        if vlan_name:
            entry["name"] = vlan_name
        entries.append(entry)
    return entries


def build_topology() -> dict:
    """The network as the model is allowed to see it."""
    devices = device_service.list_devices()
    snapshots = {device.id: latest_snapshot(device.id) for device in devices}
    management_ips = _management_ips(devices)

    networks: list[dict] = []
    for device in devices:
        for entry in _networks_for(device, snapshots[device.id]):
            if _hides_a_management_ip(entry["subnet"], management_ips):
                continue
            networks.append(entry)

    networks.sort(key=lambda entry: (entry["vlan_id"], entry["gateway_device"]))

    return {"networks": networks, "routing": []}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_topology.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/ai/topology.py backend/tests/ai/test_topology.py
git commit -m "feat: derive a VLAN/subnet/gateway map from monitoring snapshots"
```

---

### Task 2: Per-router routes, so the model can place a rule

**Files:**
- Modify: `backend/src/network_copilot/ai/topology.py` (add `_routing_for`, populate the `"routing"` key)
- Test: `backend/tests/ai/test_topology.py` (append)

**Interfaces:**
- Consumes: `build_topology()` and the module-level helpers from Task 1 (`_parsed`, `ROUTE_COMMAND`, `ROUTING_ROLES`).
- Produces: `build_topology()["routing"]` — a list of `{device: str, routes: [{network: str, interface: str | None, protocol: str | None}]}`. This is what lets the model answer "which interface does guest traffic arrive on". Task 3 publishes it unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/ai/test_topology.py`:

```python
def _router_snapshot(device, routes):
    return _snapshot(device, {"show ip route": routes})


def _route(network, interface, protocol="O"):
    return {
        "network": network,
        "protocol": protocol,
        "next_hop": "10.255.1.6",
        "interface": interface,
        "distance": 110,
        "metric": 2,
    }


def test_routing_reports_how_a_router_reaches_a_known_network(app, make_device):
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    router = make_device("INTERNAL-RTR", "10.10.10.11", "core")
    _dist_snapshot(switch)
    _router_snapshot(router, [_route("10.10.60.0/24", "GigabitEthernet0/2")])

    routing = build_topology()["routing"]

    assert routing == [
        {
            "device": "INTERNAL-RTR",
            "routes": [
                {
                    "network": "10.10.60.0/24",
                    "interface": "GigabitEthernet0/2",
                    "protocol": "O",
                }
            ],
        }
    ]


def test_routing_skips_networks_that_are_not_in_the_map(app, make_device):
    """Transit /30 links and the default route are noise, and the management
    network was already filtered out of "networks" - it must not reappear here."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    router = make_device("INTERNAL-RTR", "10.10.10.11", "core")
    _dist_snapshot(switch)
    _router_snapshot(
        router,
        [
            _route("10.10.60.0/24", "GigabitEthernet0/2"),
            _route("10.255.1.4/30", "GigabitEthernet0/2", protocol="C"),
            _route("0.0.0.0/0", "GigabitEthernet0/0", protocol="S"),
            _route("10.10.10.0/24", "GigabitEthernet0/3", protocol="C"),
        ],
    )

    networks = [row["network"] for row in build_topology()["routing"][0]["routes"]]
    assert networks == ["10.10.60.0/24"]


def test_routing_only_covers_routing_roles(app, make_device):
    """An access switch has no routing table worth showing."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    access = make_device("ACC-SW1", "10.10.10.31", "access")
    _dist_snapshot(switch)
    _router_snapshot(access, [_route("10.10.60.0/24", "Vlan60")])

    assert [entry["device"] for entry in build_topology()["routing"]] == []


def test_a_router_with_no_relevant_routes_is_omitted(app, make_device):
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    router = make_device("INTERNAL-RTR", "10.10.10.11", "core")
    _dist_snapshot(switch)
    _router_snapshot(router, [_route("10.255.1.4/30", "GigabitEthernet0/2")])

    assert build_topology()["routing"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_topology.py -v -k routing` (from `backend/`)
Expected: FAIL — `assert [] == [{'device': 'INTERNAL-RTR', ...}]` on the first, because `build_topology()` still hardcodes `"routing": []`. (`test_routing_only_covers_routing_roles` and `test_a_router_with_no_relevant_routes_is_omitted` already pass against the empty list and must keep passing.)

- [ ] **Step 3: Implement the routing section**

In `backend/src/network_copilot/ai/topology.py`, add this function immediately above `build_topology`:

```python
def _routing_for(devices, snapshots, networks) -> list[dict]:
    """How each router reaches the networks in the map.

    Restricted to networks already in "networks", which means the
    management filter applied there covers this too: a route to a filtered
    network cannot reappear through this door. Transit links and the
    default route are dropped as noise.
    """
    known = {entry["subnet"] for entry in networks}

    routing = []
    for device in devices:
        if device.role not in ROUTING_ROLES:
            continue
        routes = [
            {
                "network": row["network"],
                "interface": row.get("interface"),
                "protocol": row.get("protocol"),
            }
            for row in _parsed(snapshots[device.id], ROUTE_COMMAND)
            if row.get("network") in known
        ]
        if routes:
            routing.append({"device": device.hostname, "routes": routes})
    return routing
```

Then find the return statement at the end of `build_topology`:

```python
    return {"networks": networks, "routing": []}
```

Replace with:

```python
    return {
        "networks": networks,
        "routing": _routing_for(devices, snapshots, networks),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_topology.py -v`
Expected: PASS (14 tests — the 10 from Task 1 plus the 4 here)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/ai/topology.py backend/tests/ai/test_topology.py
git commit -m "feat: tell the AI which interface reaches each network"
```

---

### Task 3: Publish the map, and narrow the stated security rule

**Files:**
- Modify: `backend/src/network_copilot/ai/service.py` (module docstring, import, `build_context`)
- Test: `backend/tests/ai/test_ai.py` (append)

**Interfaces:**
- Consumes: `topology.build_topology() -> dict` (Tasks 1-2).
- Produces: `build_context()` returning an additional `"topology"` key holding that dict. Task 4 adds prompt rules that reference it by name.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/ai/test_ai.py`:

```python
def test_context_carries_the_network_topology(app, admin_user, make_device):
    from network_copilot.extensions import db as _db
    from network_copilot.monitoring.model import DeviceSnapshot

    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _db.session.add(
        DeviceSnapshot(
            device_id=switch.id,
            status="online",
            raw_output={},
            parsed_data={
                "show ip interface brief": [
                    {
                        "interface": "Vlan60",
                        "ip_address": "10.10.60.1",
                        "status": "up",
                        "protocol": "up",
                    }
                ],
                "show vlan brief": [
                    {"vlan_id": 60, "name": "GUEST", "status": "active", "ports": []}
                ],
                "show ip route": [
                    {
                        "network": "10.10.60.0/24",
                        "protocol": "C",
                        "next_hop": None,
                        "interface": "Vlan60",
                        "distance": None,
                        "metric": None,
                    }
                ],
            },
        )
    )
    _db.session.commit()

    service, provider = service_with(app, MONITOR_ACTION)
    service.interpret("Kiem tra OSPF cua DIST-SW1", admin_user.id)

    topology = provider.prompts[0]["context"]["topology"]
    assert topology["networks"][0]["subnet"] == "10.10.60.0/24"
    assert topology["networks"][0]["name"] == "GUEST"


def test_topology_never_leaks_a_management_ip_to_the_model(app, admin_user, make_device):
    """The narrowed rule in ai/service.py's docstring, asserted end to end:
    user subnets go to the model, management addresses never do."""
    from network_copilot.extensions import db as _db
    from network_copilot.monitoring.model import DeviceSnapshot

    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _db.session.add(
        DeviceSnapshot(
            device_id=switch.id,
            status="online",
            raw_output={},
            parsed_data={
                "show ip interface brief": [
                    {
                        "interface": "Vlan10",
                        "ip_address": "10.10.10.22",
                        "status": "up",
                        "protocol": "up",
                    }
                ],
                "show vlan brief": [
                    {"vlan_id": 10, "name": "MGMT", "status": "active", "ports": []}
                ],
                "show ip route": [
                    {
                        "network": "10.10.10.0/24",
                        "protocol": "C",
                        "next_hop": None,
                        "interface": "Vlan10",
                        "distance": None,
                        "metric": None,
                    }
                ],
            },
        )
    )
    _db.session.commit()

    service, provider = service_with(app, MONITOR_ACTION)
    service.interpret("Kiem tra OSPF cua DIST-SW1", admin_user.id)

    assert "10.10.10.0/24" not in provider.everything_sent()
    assert "10.10.10.22" not in provider.everything_sent()


def test_context_topology_is_empty_without_snapshots(app, admin_user):
    service, provider = service_with(app, MONITOR_ACTION)
    service.interpret("Kiem tra OSPF cua DIST-SW1", admin_user.id)

    assert provider.prompts[0]["context"]["topology"] == {
        "networks": [],
        "routing": [],
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "topology"` (from `backend/`)
Expected: FAIL — `KeyError: 'topology'` on the first and third. (`test_topology_never_leaks_a_management_ip_to_the_model` already passes, because nothing is sent yet; it must still pass after Step 3, which is the point of the test.)

- [ ] **Step 3: Narrow the docstring and publish the map**

In `backend/src/network_copilot/ai/service.py`, find the module docstring's first design rule:

```
* The model never receives credentials, management IPs or a full running-config.
```

Replace with:

```
* The model never receives credentials, management IPs or a full running-config.
  It does receive user network subnets and their gateways (see ai/topology.py):
  those are what policy reasoning is *about*, whereas a management IP is how you
  *reach* a device. The split is enforced by dropping any network that contains
  a Device.management_ip, so no address is hardcoded anywhere.
```

Find the last two lines of the import block (verified against the file as it
stands after the ASA work landed):

```python
from .provider import build_provider
from .schemas import AIAction, AIOperation, build_ai_action_schema
```

Replace with:

```python
from .provider import build_provider
from .schemas import AIAction, AIOperation, build_ai_action_schema
from .topology import build_topology
```

`ai/topology.py` imports `devices.service` and `monitoring.service`, neither
of which imports anything from `ai`, so this is not a circular import.

Then find the end of `build_context()`:

```python
            "supported_commands": commands,
            "asa_command_equivalents": ASA_COMMAND_EQUIVALENTS,
        }
```

Replace with:

```python
            "supported_commands": commands,
            "asa_command_equivalents": ASA_COMMAND_EQUIVALENTS,
            "topology": build_topology(),
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k "topology"`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/network_copilot/ai/service.py backend/tests/ai/test_ai.py
git commit -m "feat: publish the network topology to the AI context"
```

---

### Task 4: Four ACL rules in the system prompt

**Files:**
- Modify: `backend/src/network_copilot/ai/service.py` (`SYSTEM_PROMPT`)
- Test: `backend/tests/ai/test_ai.py` (append)

**Interfaces:**
- Consumes: the `"topology"` context key from Task 3, which two of the rules name explicitly.
- Produces: nothing consumed by another task — this is the last task.

Each rule corresponds to a specific defect observed in Batch #17.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/ai/test_ai.py`:

```python
def test_prompt_carries_the_acl_domain_rules(app, admin_user):
    """Batch #17 emitted "deny ip any any" with no ip access-group, from the
    request "chan guest ping toi it nhung it co the ping toi guest". Each
    assertion below pins the rule that prevents one part of that failure."""
    service, provider = service_with(app, MONITOR_ACTION)
    service.interpret("Kiem tra OSPF cua DIST-SW1", admin_user.id)
    prompt = provider.prompts[0]["system_prompt"]

    # Resolve names to real subnets instead of guessing.
    assert "topology.networks" in prompt
    # An unapplied access list does nothing - the Batch #17 defect.
    assert "ip access-group" in prompt
    # Without this the implicit deny kills OSPF and DHCP relay.
    assert "permit ip any any" in prompt
    # Blocking all icmp also blocks the reply of the allowed direction.
    assert "echo" in prompt
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k acl_domain` (from `backend/`)
Expected: FAIL — `assert 'topology.networks' in prompt`, because `SYSTEM_PROMPT` says nothing about ACLs today.

- [ ] **Step 3: Add the rules**

In `backend/src/network_copilot/ai/service.py`, find this rule inside `SYSTEM_PROMPT`:

```
- Never emit or decide risk, confirmation, approval, or authorization fields.
  The backend derives and enforces all of them independently.
```

Insert the four ACL rules immediately after it:

```
- Never emit or decide risk, confirmation, approval, or authorization fields.
  The backend derives and enforces all of them independently.
- Resolve network names to addresses using topology.networks. When the user
  names a network ("guest", "IT", "VLAN 60"), use that entry's "subnet" and
  never "any"; use topology.routing to choose which interface and direction
  a rule belongs on.
- An access list does nothing until it is applied to an interface with
  "ip access-group <name> in" or "out". A proposal that creates a list
  without applying it is incomplete.
- End an extended access list with "permit ip any any" unless the user
  explicitly asks to block everything else. The implicit "deny any" at the
  end of every access list otherwise drops routing protocols and DHCP relay.
- To block ping in one direction only, deny "icmp <source> <destination>
  echo". Denying all icmp also drops the echo-reply of the direction that is
  meant to keep working.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `../.venv/Scripts/python.exe -m pytest tests/ai/test_ai.py -v -k acl_domain`
Expected: PASS (1 test)

- [ ] **Step 5: Run the full backend test suite**

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass — 724 as of this plan plus the tests added across Tasks 1-4; none should fail. This confirms that adding a context key broke no existing AI, chat, dashboard or E2E test.

- [ ] **Step 6: Commit**

```bash
git add backend/src/network_copilot/ai/service.py backend/tests/ai/test_ai.py
git commit -m "feat: teach the AI how a working access list is built"
```

---

### Task 5: Live-lab verification

**Files:** none — verification only. If a defect is found, fix it in the file it belongs to and note that in the commit.

**Interfaces:** none — this exercises Tasks 1-4 together against the real lab, with a real model.

Unit tests cannot prove this feature works: they never call a model. The acceptance criterion is the original failing request succeeding.

- [ ] **Step 1: Deploy and enable monitoring**

On the AI Server:

```bash
git pull origin main
```

The map is built from monitoring snapshots, so the scheduler must be running. In the AI Server's `backend/.env`:

```
MONITORING_ENABLED=true
```

Restart the Flask process and wait one `MONITORING_INTERVAL_SECONDS` (60 by default) so snapshots exist.

- [ ] **Step 2: Confirm the map is populated**

```bash
.venv/bin/python -c "
from network_copilot.app import create_app
from network_copilot.ai.topology import build_topology
app = create_app()
with app.app_context():
    topology = build_topology()
    for entry in topology['networks']:
        print(entry)
    print('routing devices:', [r['device'] for r in topology['routing']])
"
```

Expected: entries for VLAN 20 `HR` (`10.10.20.0/24`), VLAN 30 `IT` (`10.10.30.0/24`), VLAN 60 `GUEST` (`10.10.60.0/24`), VLAN 70 `SERVER`, VLAN 90 `SERVICES`, and **no `10.10.10.0/24`**. An empty list means monitoring has not polled yet.

- [ ] **Step 3: The acceptance criterion**

In the chat page, send the exact request that produced Batch #17:

```
chan guest ping toi it nhung it co the ping toi guest
```

**Read the preview without approving it.** It must contain all four things Batch #17 lacked:

| Must contain | Guards against |
|---|---|
| `10.10.60.0` and `10.10.30.0` as source/destination | the `any any` that made the rule meaningless |
| `echo` at the end of the `deny icmp` line | blocking IT's ping too |
| `permit ip any any` | the implicit deny killing OSPF and DHCP relay |
| `ip access-group ... in` on an interface | the list sitting inert, the Batch #17 defect |

If all four are present, Approve and Apply. If any is missing, **Cancel** and record which — that is the result, and it is worth reporting either way.

- [ ] **Step 4: Verify the policy actually works**

- From **GUEST1**: `ping 10.10.30.137` → must fail with
  `ICMP type:3, code:13, Communication administratively prohibited`
- From **IT1**: `ping 10.10.60.138` → must still succeed
- On INTERNAL-RTR: `show access-lists` → the `deny` line must show a
  rising `(N matches)` count. Matches are the proof the list is attached;
  a list that exists but never matches is the Batch #17 failure again.

- [ ] **Step 5: Confirm no regression in ordinary requests**

```
Kiem tra OSPF cua DIST-SW1
```

Expected: unchanged behaviour — the monitor path still returns the neighbour table. The context grew a key; nothing about existing intents should shift.

- [ ] **Step 6: Report**

Record the outcome. If the plain-language request now produces a correct ACL, the feature has met its goal: the copilot became usable by someone who does not know the network's addressing. If it does not, capture the preview verbatim — that is the evidence for what the model still cannot infer, and it belongs in the evaluation document either way.
