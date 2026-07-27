from fixtures import GARBAGE, IP_ROUTE

from network_copilot.parsers.routes import parse_ip_routes


def test_parses_the_default_route():
    rows = parse_ip_routes(IP_ROUTE)
    default_route = next(row for row in rows if row["network"] == "0.0.0.0/0")
    assert default_route["protocol"] == "S"
    assert default_route["next_hop"] == "10.255.0.1"


def test_parses_a_connected_route():
    rows = parse_ip_routes(IP_ROUTE)
    route = next(row for row in rows if row["network"] == "10.10.10.0/24")
    assert route["protocol"] == "C"
    assert route["interface"] == "GigabitEthernet0/1"
    assert route["next_hop"] is None


def test_parses_an_ospf_route():
    rows = parse_ip_routes(IP_ROUTE)
    route = next(row for row in rows if row["network"] == "10.10.20.0/24")
    assert route["protocol"] == "O"
    assert route["next_hop"] == "10.255.0.6"
    assert route["interface"] == "GigabitEthernet0/2"


def test_parses_multi_token_protocol_codes():
    rows = parse_ip_routes(IP_ROUTE)
    inter_area = next(row for row in rows if row["network"] == "10.10.30.0/24")
    assert inter_area["protocol"] == "O IA"
    external = next(row for row in rows if row["network"] == "172.16.0.0/16")
    assert external["protocol"] == "O E2"


def test_host_route_without_a_mask_gets_one():
    rows = parse_ip_routes(IP_ROUTE)
    assert any(row["network"] == "1.1.1.1/32" for row in rows)


def test_header_and_codes_block_are_skipped():
    rows = parse_ip_routes(IP_ROUTE)
    networks = [row["network"] for row in rows]
    assert "Codes:" not in networks
    assert len(rows) == 7


def test_empty_and_invalid_input():
    assert parse_ip_routes("") == []
    assert parse_ip_routes(None) == []
    assert parse_ip_routes(GARBAGE) == []
