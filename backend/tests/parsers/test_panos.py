from network_copilot.parsers import parse_command_output
from network_copilot.parsers.panos import (
    parse_panos_interfaces,
    parse_panos_routes,
    parse_panos_system_info,
)

SYSTEM_INFO = """
hostname: PA-FW1
ip-address: 172.16.3.123
public-ip-address: unknown
netmask: 255.255.255.0
default-gateway: 172.16.3.1
mac-address: 50:00:00:04:00:00
time: Mon Oct 26 09:15:01 2026
uptime: 0 days, 0:21:07
family: vm
model: PA-VM
serial: unknown
sw-version: 10.1.0
operational-mode: normal
"""

INTERFACES = """
total configured hardware interfaces: 3

name                    id    speed/duplex/state            mac address
--------------------------------------------------------------------------------
ethernet1/1             16    10000/full/up                 50:00:00:04:00:01
ethernet1/2             17    10000/full/down               50:00:00:04:00:02
ethernet1/3             18    10000/full/up                 50:00:00:04:00:03

aggregation groups: 0

total configured logical interfaces: 3

name                id    vsys zone             forwarding               tag    address
------------------- ----- ---- ---------------- ------------------------ ------ ------------------
ethernet1/1         16    1    trust            vr:default               0      10.10.10.1/24
ethernet1/2         17    1                     N/A                      0      N/A
ethernet1/3         18    1    AITOOL-MGMT      vr:default               0      172.16.3.123/24
"""

ROUTES = """
flags: A:active, ?:loose, C:connect, H:host, S:static, ~:internal, R:rip, O:ospf, B:bgp,
       Oi:ospf intra-area, Oo:ospf inter-area, O1:ospf ext-type-1, O2:ospf ext-type-2, E:ecmp, M:multicast


VIRTUAL ROUTER: default (id 1)
  ==========
destination                                 nexthop                                 metric flags      age   interface          next-AS
0.0.0.0/0                                   172.16.3.1                              10     A S              ethernet1/3
10.10.10.0/24                               10.10.10.1                              0      A C              ethernet1/1
10.10.10.1/32                               0.0.0.0                                 0      A H
192.168.50.0/24                             10.10.10.2                              10     A Oi       1205  ethernet1/1
total routes shown: 4

VIRTUAL ROUTER: lab (id 2)
  ==========
destination                                 nexthop                                 metric flags      age   interface          next-AS
172.31.0.0/16                               discard                                 10     ?  S
total routes shown: 1
"""


def test_system_info_is_a_single_record_of_facts():
    [facts] = parse_panos_system_info(SYSTEM_INFO)
    assert facts["hostname"] == "PA-FW1"
    assert facts["ip-address"] == "172.16.3.123"
    assert facts["sw-version"] == "10.1.0"
    # values that contain colons are kept whole
    assert facts["mac-address"] == "50:00:00:04:00:00"
    assert facts["time"] == "Mon Oct 26 09:15:01 2026"
    assert parse_panos_system_info("") == []


def test_interfaces_merge_hardware_and_logical_rows():
    rows = {r["interface"]: r for r in parse_panos_interfaces(INTERFACES)}
    assert list(rows) == ["ethernet1/1", "ethernet1/2", "ethernet1/3"]
    mgmt = rows["ethernet1/3"]
    assert (mgmt["state"], mgmt["speed"], mgmt["duplex"]) == ("up", "10000", "full")
    assert (mgmt["zone"], mgmt["forwarding"]) == ("AITOOL-MGMT", "vr:default")
    assert (mgmt["ip_address"], mgmt["prefix_length"]) == ("172.16.3.123", 24)
    assert mgmt["mac"] == "50:00:00:04:00:03"
    # an empty zone column does not shift the columns after it
    down = rows["ethernet1/2"]
    assert down["zone"] is None and down["forwarding"] == "N/A" and down["ip_address"] is None
    assert down["status"] == "down"


def test_routes_keep_flags_protocol_and_virtual_router():
    routes = parse_panos_routes(ROUTES)
    assert len(routes) == 5
    default = routes[0]
    assert default["destination"] == "0.0.0.0/0" and default["next_hop"] == "172.16.3.1"
    assert default["flags"] == ["A", "S"] and default["protocol"] == "static" and default["active"]
    assert default["interface"] == "ethernet1/3" and default["virtual_router"] == "default"
    host = routes[2]
    assert host["protocol"] == "local" and host["next_hop"] is None and host["interface"] is None
    ospf = routes[3]
    assert ospf["protocol"] == "ospf" and ospf["age"] == 1205 and ospf["interface"] == "ethernet1/1"
    blackhole = routes[4]
    assert blackhole["virtual_router"] == "lab" and not blackhole["active"]
    assert blackhole["next_hop"] is None and blackhole["prefix_length"] == 16


def test_panos_commands_are_dispatched_to_their_parsers():
    assert parse_command_output("show system info", SYSTEM_INFO)[0]["model"] == "PA-VM"
    assert len(parse_command_output("show  interface   all", INTERFACES)) == 3
    assert parse_command_output("SHOW ROUTING ROUTE", ROUTES)[1]["protocol"] == "connected"
