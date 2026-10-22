import pytest
from pydantic import ValidationError

from network_copilot.ai.schemas import NetworkDiagram, diagram_schema


def test_diagram_preserves_device_and_link_addresses():
    diagram = NetworkDiagram.model_validate({
        "title": "Branch",
        "nodes": [
            {"id": "r1", "label": "R1", "kind": "router", "ip_addresses": ["10.0.0.1/30"]},
            {"id": "r2", "label": "R2", "kind": "router", "ip_addresses": ["10.0.0.2/30"]},
        ],
        "links": [{
            "source": "r1",
            "target": "r2",
            "network": "10.0.0.0/30",
            "ip_source": "10.0.0.1/30",
            "ip_target": "10.0.0.2/30",
        }],
    })

    assert diagram.nodes[0].ip_addresses == ["10.0.0.1/30"]
    assert diagram.links[0].network == "10.0.0.0/30"
    assert diagram.links[0].ip_source == "10.0.0.1/30"


@pytest.mark.parametrize("address", ["not-an-ip", "10.0.0.999", "10.0.0.1/33"])
def test_diagram_rejects_invalid_device_addresses(address):
    with pytest.raises(ValidationError):
        NetworkDiagram.model_validate({
            "title": "Branch",
            "nodes": [
                {"id": "r1", "label": "R1", "kind": "router", "ip_addresses": [address]},
                {"id": "r2", "label": "R2", "kind": "router"},
            ],
            "links": [{"source": "r1", "target": "r2"}],
        })


def test_provider_schema_accepts_optional_topology_addresses():
    schema = diagram_schema()
    node_array = schema["properties"]["nodes"]
    link_array = schema["properties"]["links"]
    node_properties = schema["properties"]["nodes"]["items"]["properties"]
    link_properties = schema["properties"]["links"]["items"]["properties"]

    assert node_array["minItems"] == 2
    assert node_array["maxItems"] == 16
    assert link_array["minItems"] == 1
    assert link_array["maxItems"] == 32
    assert node_properties["ip_addresses"]["type"] == "array"
    assert link_properties["network"]["type"] == "string"
    assert link_properties["ip_source"]["type"] == "string"
    assert link_properties["ip_target"]["type"] == "string"
