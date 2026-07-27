from fixtures import (
    GARBAGE,
    IP_INTERFACE_BRIEF,
    IP_INTERFACE_BRIEF_WIDE_SPACING,
)

from network_copilot.parsers.interfaces import parse_ip_interface_brief

RAW_OUTPUT = IP_INTERFACE_BRIEF


def test_parse_ip_interface_brief():
    rows = parse_ip_interface_brief(RAW_OUTPUT)
    assert rows[0] == {
        "interface": "GigabitEthernet0/0",
        "ip_address": "10.255.0.2",
        "status": "up",
        "protocol": "up",
    }


def test_parses_every_interface():
    rows = parse_ip_interface_brief(RAW_OUTPUT)
    assert len(rows) == 6
    assert [row["interface"] for row in rows][-1] == "Vlan1"


def test_administratively_down_is_normalised():
    rows = parse_ip_interface_brief(RAW_OUTPUT)
    row = next(r for r in rows if r["interface"] == "GigabitEthernet0/2")
    assert row["status"] == "administratively down"
    assert row["protocol"] == "down"


def test_unassigned_ip_is_preserved():
    rows = parse_ip_interface_brief(RAW_OUTPUT)
    row = next(r for r in rows if r["interface"] == "Vlan1")
    assert row["ip_address"] == "unassigned"


def test_tolerates_wide_and_irregular_spacing():
    rows = parse_ip_interface_brief(IP_INTERFACE_BRIEF_WIDE_SPACING)
    assert rows == [
        {
            "interface": "GigabitEthernet0/0",
            "ip_address": "10.255.0.2",
            "status": "up",
            "protocol": "up",
        }
    ]


def test_header_row_is_skipped():
    rows = parse_ip_interface_brief(RAW_OUTPUT)
    assert all(row["interface"] != "Interface" for row in rows)


def test_empty_input_returns_empty_list():
    assert parse_ip_interface_brief("") == []
    assert parse_ip_interface_brief("   \n  ") == []


def test_unparseable_output_returns_empty_list():
    assert parse_ip_interface_brief(GARBAGE) == []


def test_none_input_returns_empty_list():
    assert parse_ip_interface_brief(None) == []
