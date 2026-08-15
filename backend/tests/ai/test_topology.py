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


# -- routing -----------------------------------------------------------------


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


def _routes_of(routing, hostname):
    return next(
        (entry["routes"] for entry in routing if entry["device"] == hostname), None
    )


def test_routing_reports_how_a_router_reaches_a_known_network(app, make_device):
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    router = make_device("INTERNAL-RTR", "10.10.10.11", "core")
    _dist_snapshot(switch)
    _router_snapshot(router, [_route("10.10.60.0/24", "GigabitEthernet0/2")])

    routing = build_topology()["routing"]

    assert _routes_of(routing, "INTERNAL-RTR") == [
        {
            "network": "10.10.60.0/24",
            "interface": "GigabitEthernet0/2",
            "protocol": "O",
        }
    ]


def test_the_gateway_switch_also_reports_its_connected_route(app, make_device):
    """DIST-SW2 is itself a routing role, and its connected route is how the
    model learns where the network physically attaches. It belongs in the
    map alongside the upstream router, not instead of it."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    _dist_snapshot(switch)

    routing = build_topology()["routing"]

    assert _routes_of(routing, "DIST-SW2") == [
        {"network": "10.10.60.0/24", "interface": "Vlan60", "protocol": "C"}
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
    """An access switch has no routing table worth showing, even when it
    reports a route to a network that is in the map."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    access = make_device("ACC-SW1", "10.10.10.31", "access")
    _dist_snapshot(switch)
    _router_snapshot(access, [_route("10.10.60.0/24", "Vlan60")])

    devices = [entry["device"] for entry in build_topology()["routing"]]
    assert "ACC-SW1" not in devices
    assert "DIST-SW2" in devices


def test_a_router_with_no_relevant_routes_is_omitted(app, make_device):
    """INTERNAL-RTR knows only a transit /30 here, which is not in the map,
    so it contributes nothing and is left out entirely."""
    switch = make_device("DIST-SW2", "10.10.10.22", "distribution")
    router = make_device("INTERNAL-RTR", "10.10.10.11", "core")
    _dist_snapshot(switch)
    _router_snapshot(router, [_route("10.255.1.4/30", "GigabitEthernet0/2")])

    devices = [entry["device"] for entry in build_topology()["routing"]]
    assert "INTERNAL-RTR" not in devices
