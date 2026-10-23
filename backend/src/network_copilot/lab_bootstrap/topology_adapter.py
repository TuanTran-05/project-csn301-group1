"""Small, side-effect-free adapter for the supported topology-data contract.

This mirrors the field projection and relationship checks used by
``topology.pnetlab_import.normalize_topology_data`` without importing its
Project, database, or PNETLab dependencies. It is deliberately limited to the
node/topology shapes consumed by the offline planner and archive inspector.
"""

from __future__ import annotations

import re


_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class TopologyDataError(ValueError):
    """Topology data falls outside the supported offline normalization contract."""


def _resolve_node_reference(reference: str, nodes: dict) -> str | None:
    known = {str(key) for key in nodes}
    if reference in known:
        return reference
    if reference.startswith("node") and reference[4:] in known:
        return reference[4:]
    return None


def _bounded_text(value, label: str, limit: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TopologyDataError(f"PNETLab returned invalid {label}.")
    return str(value)[:limit]


def normalize_topology_data(payload: dict) -> tuple[dict, list]:
    """Normalize only the importer-compatible node and relationship fields.

    Parser results with unsupported archive metadata remain unverified and are
    not made planner-eligible by this adapter. Callers must enforce their own
    provenance/compatibility gate before planning.
    """
    if not isinstance(payload, dict) or set(payload) != {"nodes", "topology"}:
        raise TopologyDataError("PNETLab topology must contain only nodes and topology.")
    raw_nodes, raw_topology = payload["nodes"], payload["topology"]
    if not isinstance(raw_nodes, dict) or len(raw_nodes) > 500:
        raise TopologyDataError("PNETLab returned invalid or excessive node data.")
    if not isinstance(raw_topology, list) or len(raw_topology) > 10_000:
        raise TopologyDataError("PNETLab returned invalid or excessive topology data.")

    nodes = {}
    for raw_id, raw in raw_nodes.items():
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise TopologyDataError("PNETLab returned an invalid node identifier or record.")
        node_id = str(raw_id)
        if not _NODE_ID.fullmatch(node_id) or not isinstance(raw, dict):
            raise TopologyDataError("PNETLab returned an invalid node identifier or record.")
        if node_id in nodes:
            raise TopologyDataError("PNETLab returned node IDs that collide after normalization.")
        node = {}
        for field in ("name", "template", "image", "image_build"):
            value = raw.get(field)
            if value is not None:
                node[field] = _bounded_text(
                    value, "node metadata", 512 if field == "image" else 128
                )
        for field in ("left", "top"):
            value = raw.get(field)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise TopologyDataError("PNETLab returned invalid node coordinates.")
                node[field] = str(value)[:32]
        nodes[node_id] = node

    topology = []
    for raw in raw_topology:
        if not isinstance(raw, dict):
            raise TopologyDataError("PNETLab returned an invalid topology entry.")
        entry = {}
        for field, limit in (
            ("source", 32), ("destination", 32),
            ("source_type", 16), ("destination_type", 16),
            ("source_label", 64), ("destination_label", 64),
        ):
            value = raw.get(field)
            if value is not None:
                entry[field] = _bounded_text(value, "link metadata", limit)

        source_type = entry.get("source_type")
        destination_type = entry.get("destination_type")
        if (source_type, destination_type) not in {
            ("node", "node"), ("node", "network"), ("network", "node"),
        }:
            raise TopologyDataError(
                "PNETLab returned an unsupported or incomplete topology relationship."
            )
        for endpoint, endpoint_type in (
            ("source", source_type), ("destination", destination_type),
        ):
            reference = entry.get(endpoint, "")
            if endpoint_type == "node" and _resolve_node_reference(reference, nodes) is None:
                raise TopologyDataError("PNETLab topology references an unknown node.")
            if endpoint_type == "network" and not _NODE_ID.fullmatch(reference):
                raise TopologyDataError("PNETLab returned an invalid network identifier.")
        topology.append(entry)
    return nodes, topology
