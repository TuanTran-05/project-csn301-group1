from fixtures import GARBAGE, VLAN_BRIEF

from network_copilot.parsers.vlans import parse_vlan_brief


def test_parse_vlan_brief():
    rows = parse_vlan_brief(VLAN_BRIEF)
    assert rows[0] == {
        "vlan_id": 1,
        "name": "default",
        "status": "active",
        "ports": ["Gi0/3", "Gi1/0", "Gi1/1", "Gi1/2", "Gi1/3", "Gi2/0", "Gi2/1"],
    }


def test_parses_every_vlan():
    rows = parse_vlan_brief(VLAN_BRIEF)
    assert [row["vlan_id"] for row in rows] == [1, 10, 20, 25, 99, 1002, 1003]


def test_vlan_id_is_an_integer():
    rows = parse_vlan_brief(VLAN_BRIEF)
    assert all(isinstance(row["vlan_id"], int) for row in rows)


def test_vlan_without_ports_has_an_empty_list():
    rows = parse_vlan_brief(VLAN_BRIEF)
    marketing = next(row for row in rows if row["vlan_id"] == 25)
    assert marketing["name"] == "MARKETING"
    assert marketing["ports"] == []


def test_continuation_lines_extend_the_previous_vlan():
    rows = parse_vlan_brief(VLAN_BRIEF)
    default_vlan = next(row for row in rows if row["vlan_id"] == 1)
    assert "Gi2/1" in default_vlan["ports"]


def test_unsupported_status_is_preserved():
    rows = parse_vlan_brief(VLAN_BRIEF)
    assert next(row for row in rows if row["vlan_id"] == 1002)["status"] == "act/unsup"


def test_separator_row_is_skipped():
    rows = parse_vlan_brief(VLAN_BRIEF)
    assert all(not row["name"].startswith("---") for row in rows)


def test_empty_and_invalid_input():
    assert parse_vlan_brief("") == []
    assert parse_vlan_brief(None) == []
    assert parse_vlan_brief(GARBAGE) == []
