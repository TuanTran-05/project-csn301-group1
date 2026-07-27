from fixtures import GARBAGE, OSPF_NEIGHBOR, OSPF_NEIGHBOR_EMPTY

from network_copilot.parsers.ospf import parse_ospf_neighbors


def test_parse_ospf_neighbors():
    rows = parse_ospf_neighbors(OSPF_NEIGHBOR)
    assert rows[0] == {
        "neighbor_id": "2.2.2.2",
        "priority": 1,
        "state": "FULL/DR",
        "dead_time": "00:00:33",
        "address": "10.255.0.6",
        "interface": "GigabitEthernet0/2",
    }


def test_parses_every_neighbor():
    assert len(parse_ospf_neighbors(OSPF_NEIGHBOR)) == 4


def test_state_with_spaces_is_normalised():
    rows = parse_ospf_neighbors(OSPF_NEIGHBOR)
    row = next(r for r in rows if r["neighbor_id"] == "4.4.4.4")
    assert row["state"] == "FULL/-"
    assert row["priority"] == 0


def test_non_full_states_are_reported():
    rows = parse_ospf_neighbors(OSPF_NEIGHBOR)
    row = next(r for r in rows if r["neighbor_id"] == "5.5.5.5")
    assert row["state"] == "2WAY/DROTHER"


def test_priority_is_an_integer():
    rows = parse_ospf_neighbors(OSPF_NEIGHBOR)
    assert all(isinstance(row["priority"], int) for row in rows)


def test_header_only_output_returns_empty_list():
    assert parse_ospf_neighbors(OSPF_NEIGHBOR_EMPTY) == []


def test_empty_and_invalid_input():
    assert parse_ospf_neighbors("") == []
    assert parse_ospf_neighbors(None) == []
    assert parse_ospf_neighbors(GARBAGE) == []
