import pytest

from network_copilot.lab_bootstrap.interfaces import normalize_label
from network_copilot.lab_bootstrap.topology_adapter import (
    TopologyDataError,
    normalize_topology_data,
)


def test_pure_adapter_preserves_importer_node_fields_and_truncation_contract():
    nodes, topology = normalize_topology_data({
        "nodes": {
            "1": {
                "name": "Router 1",
                "template": "Cisco vIOS Router",
                "image": "x" * 520,
                "image_build": "15.9-build",
                "left": 12.5,
                "top": "40%",
                "qemu_version": "not part of the planner contract",
            },
        },
        "topology": [],
    })

    assert nodes == {
        "1": {
            "name": "Router 1",
            "template": "Cisco vIOS Router",
            "image": "x" * 512,
            "image_build": "15.9-build",
            "left": "12.5",
            "top": "40%",
        },
    }
    assert topology == []


@pytest.mark.parametrize(
    ("source_type", "destination_type", "source", "destination"),
    [
        ("node", "node", "node1", "2"),
        ("node", "network", "1", "net1"),
        ("network", "node", "net1", "node2"),
    ],
)
def test_pure_adapter_preserves_supported_relationship_shapes(
    source_type, destination_type, source, destination,
):
    nodes, topology = normalize_topology_data({
        "nodes": {"1": {}, "2": {}},
        "topology": [{
            "source": source,
            "source_type": source_type,
            "source_label": "Gi0/0",
            "destination": destination,
            "destination_type": destination_type,
            "destination_label": "Gi0/1",
            "ignored": "extra per-link metadata is projected out",
        }],
    })

    assert set(nodes) == {"1", "2"}
    assert topology == [{
        "source": source,
        "source_type": source_type,
        "source_label": "Gi0/0",
        "destination": destination,
        "destination_type": destination_type,
        "destination_label": "Gi0/1",
    }]


@pytest.mark.parametrize(
    "payload",
    [
        {"nodes": {"1": {}}, "topology": [{"source_type": "node"}]},
        {"nodes": {"1": {}}, "topology": [{
            "source": "missing", "source_type": "node",
            "destination": "1", "destination_type": "node",
        }]},
        {"nodes": {"1": {}}, "topology": [{
            "source": "1", "source_type": "node",
            "destination": "1", "destination_type": "cloud",
        }]},
        {"nodes": {"1": {}}, "topology": [], "database_payload": True},
    ],
)
def test_pure_adapter_rejects_incomplete_relationships_unknown_refs_and_extra_contract_fields(payload):
    with pytest.raises(TopologyDataError):
        normalize_topology_data(payload)


def test_pure_adapter_rejects_node_ids_that_collide_after_string_normalization():
    with pytest.raises(TopologyDataError, match="collide after normalization"):
        normalize_topology_data({
            "nodes": {1: {"name": "numeric"}, "1": {"name": "string"}},
            "topology": [],
        })


@pytest.mark.parametrize("raw_id", [None, True, 1.5])
def test_pure_adapter_rejects_node_ids_that_are_not_strings_or_integers(raw_id):
    with pytest.raises(TopologyDataError, match="invalid node identifier"):
        normalize_topology_data({"nodes": {raw_id: {}}, "topology": []})


@pytest.mark.parametrize(
    "payload",
    [
        {"nodes": {"1": {"name": True}}, "topology": []},
        {"nodes": {"1": {"left": True}}, "topology": []},
        {"nodes": {"1": {}}, "topology": [{
            "source": True, "source_type": "node",
            "destination": "1", "destination_type": "node",
        }]},
    ],
)
def test_pure_adapter_rejects_boolean_metadata_and_relationship_values(payload):
    with pytest.raises(TopologyDataError):
        normalize_topology_data(payload)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("e0/1", "Ethernet0/1"),
        ("Gi 0/1", "GigabitEthernet0/1"),
        ("gigabitethernet0/1", "GigabitEthernet0/1"),
        ("te0/1", "TenGigabitEthernet0/1"),
        ("mystery0/1", None),
        (None, None),
    ],
)
def test_offline_interface_normalizer_matches_supported_alias_contract(label, expected):
    assert normalize_label(label) == expected
