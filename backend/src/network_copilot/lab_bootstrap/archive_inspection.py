"""Build an inspection-only topology candidate from a parsed PNETLab archive.

The current archive parser deliberately reports an unverified schema. This
adapter preserves explicit cable and network-attachment evidence for review,
but never turns that analysis into an IP plan or a verified topology.
"""

from __future__ import annotations

from .planner import BootstrapPlanError, inspect_segments
from .topology_adapter import TopologyDataError, normalize_topology_data

_VERIFIED_ARCHIVE_STATUS = "ANALYZED_VERIFIED_SCHEMA"


def _interface_label(endpoint: dict, node_interfaces: dict[str, dict[str, str | None]]) -> str:
    label = endpoint.get("interface")
    label = label.strip() if isinstance(label, str) else ""
    interface_id = endpoint.get("interface_id")
    declared_interfaces = node_interfaces.get(str(endpoint.get("id")), {})
    if interface_id is not None:
        interface_key = str(interface_id)
        if interface_key not in declared_interfaces:
            raise ValueError("A link references an undeclared node interface.")
        declared_label = declared_interfaces[interface_key] or ""
        if label and declared_label and label != declared_label:
            raise ValueError("A link interface name conflicts with node metadata.")
        return label or declared_label
    if label and label not in declared_interfaces.values():
        raise ValueError("A link references an undeclared node interface.")
    return label


def inspect_archive_analysis(analysis: dict) -> dict:
    """Return normalized segment inspection while keeping planner eligibility gated.

    Only declared node-to-node links and explicit node/interface-to-network
    mappings become relationships. Networks themselves never imply a cable.
    """
    blockers = []
    issues = []
    if not isinstance(analysis, dict):
        return {
            "status": "INSPECTION_ONLY",
            "planner_eligible": False,
            "relationship_count": 0,
            "segments": [],
            "blockers": ["Archive analysis result is not an object."],
            "issues": [],
        }

    if analysis.get("status") != _VERIFIED_ARCHIVE_STATUS:
        blockers.append("The .unl schema has not been verified for planner use.")
    if analysis.get("preview_compatible") is not True:
        blockers.append("The archive parser did not mark this topology preview-compatible.")
    warnings = analysis.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
        blockers.append("Archive analysis warnings are missing or malformed; compatibility cannot be established.")
    elif any(
        "additional metadata omitted" in warning.casefold()
        or "unsupported pnetlab metadata is omitted" in warning.casefold()
        for warning in warnings
    ):
        blockers.append("The archive contains metadata omitted by the parser; topology compatibility is incomplete.")

    try:
        source_nodes = analysis.get("nodes")
        source_networks = analysis.get("networks")
        source_links = analysis.get("links")
        interface_networks = analysis.get("interface_networks")
        if not isinstance(source_nodes, list) or not source_nodes:
            raise ValueError("Archive analysis has no node records for topology inspection.")
        if not isinstance(source_networks, list):
            raise ValueError("Archive analysis has invalid network records.")
        if not isinstance(source_links, list) or not isinstance(interface_networks, list):
            raise ValueError("Archive analysis has invalid link or interface-network records.")

        nodes = {}
        node_interfaces: dict[str, dict[str, str | None]] = {}
        for node in source_nodes:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                raise ValueError("Archive analysis contains an invalid node record.")
            node_id = node["id"]
            if node_id in nodes:
                raise ValueError("Archive analysis contains duplicate node IDs.")
            nodes[node_id] = {
                field: node[field]
                for field in ("name", "template", "image", "image_build")
                if field in node
            }
            if not isinstance(node.get("template"), str) or not node["template"].strip():
                raise ValueError(
                    f"Node {node_id} has no template metadata; IP interface capability cannot be classified."
                )
            interfaces = node.get("interfaces")
            if not isinstance(interfaces, list):
                raise ValueError(f"Node {node_id} has invalid interface metadata.")
            by_id = {}
            for interface in interfaces:
                if not isinstance(interface, dict) or not isinstance(interface.get("id"), str):
                    raise ValueError(f"Node {node_id} has an invalid interface record.")
                if interface["id"] in by_id:
                    raise ValueError(f"Node {node_id} has duplicate interface IDs.")
                label = interface.get("name")
                if label is not None and not isinstance(label, str):
                    raise ValueError(f"Node {node_id} has an invalid interface name.")
                by_id[interface["id"]] = label
            node_interfaces[node_id] = by_id

        network_ids = set()
        for network in source_networks:
            if not isinstance(network, dict) or not isinstance(network.get("id"), str):
                raise ValueError("Archive analysis contains an invalid network record.")
            if network["id"] in network_ids:
                raise ValueError("Archive analysis contains duplicate network IDs.")
            network_ids.add(network["id"])

        topology = []
        attachments = {}
        ports_to_networks = {}

        def add_attachment(node_id, interface_id, label, network_id):
            if node_id not in nodes:
                raise ValueError("A network attachment references an unknown node.")
            if network_id not in network_ids:
                raise ValueError("A network attachment references an unknown network.")
            interface_id = None if interface_id is None else str(interface_id)
            endpoint = {
                "id": node_id,
                "interface_id": interface_id,
                "interface": label,
            }
            port_label = _interface_label(endpoint, node_interfaces)
            port_key = (node_id, port_label.casefold() if port_label else f"id:{interface_id or ''}")
            previous_network = ports_to_networks.get(port_key)
            if previous_network is not None and previous_network != network_id:
                raise ValueError("An interface is attached to more than one archive network.")
            ports_to_networks[port_key] = network_id
            attachment_key = (node_id, network_id, port_key[1])
            if attachment_key in attachments:
                return
            attachments[attachment_key] = True
            topology.append({
                "source": node_id,
                "source_type": "node",
                "source_label": port_label,
                "destination": network_id,
                "destination_type": "network",
            })

        for link in source_links:
            if not isinstance(link, dict) or not isinstance(link.get("endpoints"), list):
                raise ValueError("Archive analysis contains an invalid link record.")
            endpoints = link["endpoints"]
            if len(endpoints) != 2 or any(not isinstance(item, dict) for item in endpoints):
                raise ValueError("Archive link must contain exactly two endpoint records.")
            left, right = endpoints
            kinds = (left.get("kind"), right.get("kind"))
            if link.get("kind") == "direct" and kinds == ("node", "node"):
                left_id, right_id = str(left.get("id", "")), str(right.get("id", ""))
                if left_id not in nodes or right_id not in nodes:
                    raise ValueError("A direct link references an unknown node.")
                topology.append({
                    "source": left_id,
                    "source_type": "node",
                    "source_label": _interface_label(left, node_interfaces),
                    "destination": right_id,
                    "destination_type": "node",
                    "destination_label": _interface_label(right, node_interfaces),
                })
            elif link.get("kind") == "network" and kinds in {("node", "network"), ("network", "node")}:
                node_endpoint = left if left.get("kind") == "node" else right
                network_endpoint = right if node_endpoint is left else left
                add_attachment(
                    str(node_endpoint.get("id", "")),
                    node_endpoint.get("interface_id"),
                    _interface_label(node_endpoint, node_interfaces),
                    str(network_endpoint.get("id", "")),
                )
            else:
                raise ValueError("Archive link endpoint types are unsupported or inconsistent.")

        for mapping in interface_networks:
            if not isinstance(mapping, dict):
                raise ValueError("Archive analysis contains an invalid interface-network mapping.")
            node_id = str(mapping.get("node_id", ""))
            interface_id = mapping.get("interface_id")
            label = mapping.get("interface")
            if label is not None and not isinstance(label, str):
                raise ValueError("Archive analysis contains an invalid interface label.")
            add_attachment(node_id, interface_id, label or "", str(mapping.get("network_id", "")))

        normalized_nodes, normalized_topology = normalize_topology_data({
            "nodes": nodes,
            "topology": topology,
        })
        if not normalized_topology:
            blockers.append(
                "No explicit node links or interface-to-network attachments provide connection evidence."
            )
            segments = []
        else:
            segments = inspect_segments(normalized_nodes, normalized_topology)
    except (TopologyDataError, BootstrapPlanError, TypeError, ValueError) as exc:
        issues.append(str(exc))
        normalized_topology = []
        segments = []

    planner_eligible = (
        analysis.get("status") == _VERIFIED_ARCHIVE_STATUS
        and analysis.get("preview_compatible") is True
        and not blockers
        and not issues
        and bool(normalized_topology)
    )
    return {
        "status": "INSPECTION_ONLY" if not planner_eligible else "READY_FOR_PLANNER_REVIEW",
        "planner_eligible": planner_eligible,
        "relationship_count": len(normalized_topology),
        "segments": segments,
        "blockers": blockers,
        "issues": issues,
    }
