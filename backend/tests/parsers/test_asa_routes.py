from network_copilot.parsers import parse_command_output, parse_ip_interface_brief
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
