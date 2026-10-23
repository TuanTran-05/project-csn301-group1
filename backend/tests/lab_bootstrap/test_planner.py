import pytest

from network_copilot.lab_bootstrap.planner import BootstrapPlanError, build_plan, inspect_segments
from network_copilot.lab_bootstrap.topology_adapter import normalize_topology_data


NODES = {
    "1": {"name": "R1", "template": "vios"},
    "2": {"name": "R2", "template": "vios"},
    "3": {"name": "SW1", "template": "viosl2"},
    "4": {"name": "PC1", "template": "vpcs"},
}
TOPOLOGY = [
    {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
     "destination": "net1", "destination_type": "network"},
    {"source": "node2", "source_type": "node", "source_label": "Gi0/0",
     "destination": "net1", "destination_type": "network"},
    {"source": "node1", "source_type": "node", "source_label": "Gi0/1",
     "destination": "net2", "destination_type": "network"},
    {"source": "node2", "source_type": "node", "source_label": "Gi0/1",
     "destination": "net2", "destination_type": "network"},
    {"source": "node3", "source_type": "node", "source_label": "e0/0",
     "destination": "net2", "destination_type": "network"},
    {"source": "node4", "source_type": "node", "source_label": "e0/0",
     "destination": "net2", "destination_type": "network"},
]


def test_plan_is_stable_and_keeps_pnet_node_and_port_mapping():
    kwargs = {
        "management_segment_id": "network:net1",
        "management_pool": "172.16.3.0/24",
        "data_pool": "10.100.0.0/16",
    }
    first = build_plan(NODES, TOPOLOGY, **kwargs)
    second = build_plan(NODES, TOPOLOGY, **kwargs)

    assert first == second
    assert first["status"] == "PLANNED"
    by_id = {node["node_id"]: node for node in first["nodes"]}
    assert by_id["1"]["addresses"][0] == {
        "segment_id": "network:net1", "interface": "GigabitEthernet0/0",
        "ip": "172.16.3.1", "prefix_length": 24, "subnet": "172.16.3.0/24",
        "addressing": "management", "gateway": None,
    }
    assert by_id["2"]["addresses"][1]["interface"] == "GigabitEthernet0/1"
    assert by_id["3"]["addresses"] == []
    assert by_id["4"]["addresses"][0]["interface"] == "Ethernet0/0"
    assert by_id["4"]["addresses"][0]["subnet"] == "10.100.0.0/29"
    switch_endpoint = next(
        endpoint for segment in first["segments"] for endpoint in segment["endpoints"]
        if endpoint["node_id"] == "3"
    )
    assert switch_endpoint["ip_assignable"] is False
    assert "SVI/VLAN" in switch_endpoint["addressing_note"]
    assert first["ready_to_import"] is False
    assert first["configuration"]["status"] == "NEEDS_MANUAL_ACTION"
    assert all(node["configuration_status"] == "NEEDS_MANUAL_ACTION" for node in first["nodes"])


def test_plan_normalizes_integer_node_ids_before_allocating_addresses():
    result = build_plan(
        {1: NODES["1"], 2: NODES["2"]}, TOPOLOGY[:2],
        management_segment_id="network:net1",
        management_pool="172.16.3.0/24",
    )

    assert [node["node_id"] for node in result["nodes"]] == ["1", "2"]
    assert [node["addresses"][0]["ip"] for node in result["nodes"]] == [
        "172.16.3.1", "172.16.3.2",
    ]


def test_plan_reports_normalized_node_id_collision_as_validation_error():
    with pytest.raises(BootstrapPlanError, match="collide after normalization"):
        build_plan(
            {1: NODES["1"], "1": NODES["2"]}, [],
            management_segment_id=None,
            management_pool="172.16.3.0/24",
        )


def test_plan_requires_explicit_management_segment_and_returns_candidates():
    result = build_plan(
        NODES, TOPOLOGY, management_segment_id=None,
        management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
    )
    assert result["status"] == "NEEDS_MANUAL_ACTION"
    assert result["management_segment_candidates"] == ["network:net1", "network:net2"]
    assert result["nodes"] == []


def test_shared_network_membership_is_not_a_direct_cable():
    nodes = {"1": NODES["1"], "2": NODES["2"]}
    topology = TOPOLOGY[:2]

    segments = inspect_segments(nodes, topology)
    plan = build_plan(
        nodes, topology, management_segment_id=None,
        management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
    )

    assert len(segments) == 1
    assert segments[0]["id"] == "network:net1"
    assert segments[0]["kind"] == "network"
    assert plan["management_segment_candidates"] == ["network:net1"]
    assert all(segment["kind"] != "direct" for segment in plan["segments"])


def test_plan_with_no_discovered_links_has_no_management_candidate():
    result = build_plan(
        {"1": NODES["1"], "2": NODES["2"]}, [],
        management_segment_id=None,
        management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
    )

    assert result["status"] == "NEEDS_MANUAL_ACTION"
    assert result["management_segment_candidates"] == []
    assert result["segments"] == []


def test_two_directly_connected_routers_do_not_provide_management_network():
    payload = {
        "nodes": {
            "1": {
                "name": "vIOS1", "template": "Cisco vIOS Router",
                "image": "vios-adventerprisek9-m.SPA.159-3.M6",
                "image_build": "fixture-build",
            },
            "2": {
                "name": "vIOS2", "template": "Cisco vIOS Router",
                "image": "vios-adventerprisek9-m.SPA.159-3.M6",
                "image_build": "fixture-build",
            },
        },
        "topology": [{
            "source": "node1", "source_type": "node", "source_label": "Gi0/0",
            "destination": "node2", "destination_type": "node", "destination_label": "Gi0/0",
        }],
    }

    nodes, topology = normalize_topology_data(payload)

    assert nodes == payload["nodes"]
    assert topology == payload["topology"]
    segments = inspect_segments(nodes, topology)
    assert len(segments) == 1
    assert segments[0]["id"] == "cable:0"
    assert segments[0]["kind"] == "direct"
    assert not any(segment["kind"] == "network" for segment in segments)
    assert [
        (endpoint["node_id"], endpoint["interface"], endpoint["interface_label"])
        for endpoint in segments[0]["endpoints"]
    ] == [
        ("1", "GigabitEthernet0/0", "Gi0/0"),
        ("2", "GigabitEthernet0/0", "Gi0/0"),
    ]

    plan_args = {
        "management_pool": "172.16.3.0/24",
        "data_pool": "10.100.0.0/16",
    }
    plan = build_plan(nodes, topology, management_segment_id=None, **plan_args)
    assert plan["status"] == "NEEDS_MANUAL_ACTION"
    assert plan["management_segment_candidates"] == []
    assert plan["segments"][0]["kind"] == "direct"
    assert "Direct node-to-node cables are not management candidates" in plan["issues"][0]

    with pytest.raises(BootstrapPlanError, match="not a discovered PNETLab network"):
        build_plan(nodes, topology, management_segment_id="cable:0", **plan_args)


def test_direct_link_without_interface_label_is_rejected():
    payload = {
        "nodes": {
            "1": {"name": "R1", "template": "Cisco vIOS Router"},
            "2": {"name": "R2", "template": "Cisco vIOS Router"},
        },
        "topology": [{
            "source": "node1", "source_type": "node",
            "destination": "node2", "destination_type": "node", "destination_label": "Gi0/0",
        }],
    }

    nodes, topology = normalize_topology_data(payload)

    assert "source_label" not in topology[0]
    with pytest.raises(BootstrapPlanError, match="no interface was guessed"):
        inspect_segments(nodes, topology)


def test_plan_preserves_an_explicit_image_build_without_inference():
    nodes = {
        "1": {"name": "R1", "template": "vios", "image": "image-id", "image_build": "build-17"},
        "2": {"name": "R2", "template": "vios", "image": "image-id", "image_build": "build-17"},
    }
    result = build_plan(
        nodes,
        TOPOLOGY[:2],
        management_segment_id="network:net1",
        management_pool="172.16.3.0/24",
        data_pool="10.100.0.0/16",
    )
    assert {node["image_build"] for node in result["nodes"]} == {"build-17"}
    assert {node["image_build"] for node in result["manifest"]["nodes"]} == {"build-17"}


def test_plan_rejects_credential_like_metadata_in_public_manifest_fields():
    unsafe_nodes = {"1": {"name": "Router password FIXTURE_SECRET", "template": "vios"}}
    unsafe_topology = [{
        "source": "node1", "source_type": "node", "source_label": "Gi0/0",
        "destination": "mgmt", "destination_type": "network",
    }]
    with pytest.raises(BootstrapPlanError, match="credential-like text"):
        build_plan(
            unsafe_nodes,
            unsafe_topology,
            management_segment_id="network:mgmt",
            management_pool="172.16.3.0/24",
        )


def test_plan_skips_existing_addresses_without_reusing_them():
    result = build_plan(
        NODES, TOPOLOGY, management_segment_id="network:net1",
        management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
        reserved_ips=["172.16.3.1", "10.100.0.1"],
    )
    by_id = {node["node_id"]: node for node in result["nodes"]}
    assert [row["ip"] for row in by_id["1"]["addresses"]] == ["172.16.3.2", "10.100.0.2"]
    assert len({row["ip"] for node in result["nodes"] for row in node["addresses"]}) == 5


def test_plan_rejects_ipv6_reserved_address_in_ipv4_planner():
    with pytest.raises(BootstrapPlanError, match="Existing project addresses must be IPv4"):
        build_plan(
            NODES, TOPOLOGY, management_segment_id="network:net1",
            management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
            reserved_ips=["2001:db8::1"],
        )


@pytest.mark.parametrize(
    ("reserved_networks", "expected_message"),
    [
        (["10.100.0.1/29"], "An existing project subnet is invalid"),
        (None, "Existing project subnets must be an iterable"),
    ],
)
def test_plan_rejects_invalid_or_noncanonical_reserved_subnets(
    reserved_networks, expected_message
):
    with pytest.raises(BootstrapPlanError, match=expected_message):
        build_plan(
            NODES, TOPOLOGY, management_segment_id="network:net1",
            management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
            reserved_networks=reserved_networks,
        )


def test_plan_rejects_missing_reserved_address_collection():
    with pytest.raises(BootstrapPlanError, match="Existing project addresses must be an iterable"):
        build_plan(
            NODES, TOPOLOGY, management_segment_id="network:net1",
            management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
            reserved_ips=None,
        )


def test_plan_skips_existing_project_data_subnets():
    result = build_plan(
        NODES, TOPOLOGY, management_segment_id="network:net1",
        management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
        reserved_networks=["10.100.0.0/29"],
    )
    by_id = {node["node_id"]: node for node in result["nodes"]}
    assert by_id["1"]["addresses"][1]["subnet"] == "10.100.0.8/29"


def test_management_pool_allows_its_own_project_network_reservation():
    result = build_plan(
        NODES, TOPOLOGY, management_segment_id="network:net1",
        management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
        reserved_networks=["172.16.3.0/24"],
    )

    by_id = {node["node_id"]: node for node in result["nodes"]}
    assert by_id["1"]["addresses"][0]["ip"] == "172.16.3.1"


def test_management_pool_rejects_overlap_with_another_project_subnet():
    with pytest.raises(BootstrapPlanError, match="Management pool overlaps"):
        build_plan(
            NODES, TOPOLOGY, management_segment_id="network:net1",
            management_pool="172.16.3.0/24", data_pool="10.100.0.0/16",
            reserved_networks=["172.16.3.0/25"],
        )


def test_management_pool_reserves_network_and_broadcast_and_does_not_infer_gateway():
    nodes = {"1": NODES["1"], "2": NODES["2"]}
    topology = [
        {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
         "destination": "mgmt", "destination_type": "network"},
        {"source": "node2", "source_type": "node", "source_label": "Gi0/0",
         "destination": "mgmt", "destination_type": "network"},
    ]

    result = build_plan(
        nodes, topology, management_segment_id="network:mgmt",
        management_pool="192.0.2.0/30", data_pool="10.100.0.0/16",
    )

    addresses = [node["addresses"][0] for node in result["nodes"]]
    assert [item["ip"] for item in addresses] == ["192.0.2.1", "192.0.2.2"]
    assert all(item["ip"] not in {"192.0.2.0", "192.0.2.3"} for item in addresses)
    assert all(item["gateway"] is None for item in addresses)
    assert result["gateway_status"] == "NOT_SUPPORTED"
    assert result["manifest"]["gateway_status"] == "NOT_SUPPORTED"
    assert any("no per-segment gateway or routing configuration was validated or inferred" in warning
               for warning in result["warnings"])


def test_management_pool_rejects_when_reserved_addresses_leave_too_few_hosts():
    nodes = {"1": NODES["1"], "2": NODES["2"]}
    topology = [
        {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
         "destination": "mgmt", "destination_type": "network"},
        {"source": "node2", "source_type": "node", "source_label": "Gi0/0",
         "destination": "mgmt", "destination_type": "network"},
    ]

    with pytest.raises(BootstrapPlanError, match="not have enough unreserved host addresses"):
        build_plan(
            nodes, topology, management_segment_id="network:mgmt",
            management_pool="192.0.2.0/30", data_pool="10.100.0.0/16",
            reserved_ips=["192.0.2.1"],
        )


@pytest.mark.parametrize(
    "row",
    [
        {"source": "node1", "source_label": "Gi0/0", "destination": "node2", "destination_label": "Gi0/0"},
        {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
         "destination": "unknown", "destination_type": "external"},
        {"source": "net1", "source_type": "network", "destination": "net2", "destination_type": "network"},
    ],
)
def test_segments_reject_incomplete_or_unsupported_relationships(row):
    with pytest.raises(BootstrapPlanError, match="unsupported node/link relationship"):
        inspect_segments(NODES, [row])


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"management_segment_id": "network:missing"}, "not a discovered"),
        ({"management_pool": "10.100.0.0/24"}, "overlap"),
        ({"data_pool": "10.100.0.1/24"}, "canonical"),
        ({"data_pool": "2001:db8::/64"}, "IPv4"),
        ({"data_pool": "10.100.0.0/30"}, "too small"),
    ],
)
def test_plan_rejects_invalid_segment_pools_and_capacity(kwargs, message):
    base = {
        "management_segment_id": "network:net1",
        "management_pool": "172.16.3.0/24",
        "data_pool": "10.100.0.0/16",
    }
    base.update(kwargs)
    with pytest.raises(BootstrapPlanError, match=message):
        build_plan(NODES, TOPOLOGY, **base)


def test_segments_reject_missing_interface_instead_of_guessing():
    broken = [{"source": "node1", "source_type": "node", "source_label": "",
               "destination": "net1", "destination_type": "network"}]
    with pytest.raises(BootstrapPlanError, match="no interface was guessed"):
        inspect_segments({"1": NODES["1"]}, broken)


def test_segments_reject_duplicate_node_on_same_lan():
    broken = [
        {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
         "destination": "net1", "destination_type": "network"},
        {"source": "node1", "source_type": "node", "source_label": "Gi0/1",
         "destination": "net1", "destination_type": "network"},
    ]
    with pytest.raises(BootstrapPlanError, match="more than once"):
        inspect_segments({"1": NODES["1"]}, broken)


def test_segments_reject_one_interface_attached_to_multiple_networks():
    broken = [
        {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
         "destination": "net1", "destination_type": "network"},
        {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
         "destination": "net2", "destination_type": "network"},
    ]
    with pytest.raises(BootstrapPlanError, match="multiple segments"):
        inspect_segments({"1": NODES["1"]}, broken)
