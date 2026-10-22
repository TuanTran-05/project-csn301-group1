"""Deterministic, side-effect-free IPv4 planning for discovered PNETLab topology.

This module deliberately does not generate device configuration or contact a
lab. The current topology contract does not identify exact VM image builds or
management reachability, so callers must present the IP plan for review and
must not treat it as an applied configuration.
"""

from __future__ import annotations

import ipaddress
import hashlib
import json
import re

from .interfaces import normalize_label
from .topology_adapter import TopologyDataError, normalize_topology_data

DEFAULT_DATA_POOL = "10.100.0.0/16"
MAX_NODES = 500
MAX_LINKS = 10_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SENSITIVE_METADATA = re.compile(
    r"\b(password|passwd|secret|token|api[_-]?key|authorization|credential|username|"
    r"private[_ -]?key|passphrase|community|pre[-_ ]shared[-_ ]key)\b",
    re.I,
)


class BootstrapPlanError(ValueError):
    """Invalid or ambiguous topology/addressing input; nothing was applied."""


def _node_sort(value: str):
    return (0, int(value)) if value.isdigit() else (1, value.casefold())


def _ipv4_network(value, label: str) -> ipaddress.IPv4Network:
    if not isinstance(value, str) or not value.strip():
        raise BootstrapPlanError(f"{label} must be an IPv4 CIDR network.")
    try:
        network = ipaddress.ip_network(value.strip(), strict=True)
    except ValueError as exc:
        raise BootstrapPlanError(f"{label} must be a canonical IPv4 CIDR network.") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise BootstrapPlanError(f"{label} must be IPv4.")
    if network.prefixlen > 30:
        raise BootstrapPlanError(f"{label} must be /30 or larger to represent a shared lab segment.")
    return network


def _node_id(value, known: set[str]) -> str:
    text = "" if value is None else str(value)
    if text not in known and text.startswith("node") and text[4:] in known:
        text = text[4:]
    if not _SAFE_ID.fullmatch(text) or text not in known:
        raise BootstrapPlanError("A topology link references an unknown or invalid node ID.")
    if _SENSITIVE_METADATA.search(text):
        raise BootstrapPlanError("Topology IDs contain credential-like text and cannot be exported.")
    return text


def _ip_capability(template: str) -> tuple[bool, str]:
    name = template.casefold()
    if not name:
        return False, "Image/template is missing."
    if "l2" in name or "switch" in name:
        return False, "Layer-2 switch ports do not receive host IPs; an SVI/VLAN design is required."
    if any(word in name for word in ("cloud", "internet", "nat")):
        return False, "Cloud/uplink endpoint addressing is external and is not inferred."
    if "asa" in name or any(
        word in name for word in ("vios", "iol", "csr", "c7200", "c3725", "c3745", "c1710", "router")
    ):
        return True, ""
    if any(word in name for word in ("vpcs", "docker", "desktop", "win", "pc", "linux", "ubuntu", "server", "centos", "debian")):
        return True, ""
    return False, "Unknown template; no interface IP was assigned."


def _endpoint(node_id, label, nodes: dict[str, dict]) -> dict:
    raw_label = label.strip()[:64] if isinstance(label, str) else ""
    if _SENSITIVE_METADATA.search(raw_label):
        raise BootstrapPlanError("An interface label contains credential-like text and cannot be exported.")
    interface = normalize_label(raw_label)
    node = nodes[node_id]
    template = str(node.get("template") or "")[:128]
    assignable, capability_note = _ip_capability(template)
    if assignable and interface is None:
        raise BootstrapPlanError(
            f"Node {node_id} has a missing or unsupported interface label; no interface was guessed."
        )
    return {
        "node_id": node_id,
        "name": str(node.get("name") or f"Node {node_id}")[:128],
        "template": template,
        "interface": interface,
        "interface_label": raw_label,
        "ip_assignable": assignable,
        "addressing_note": capability_note,
    }


def inspect_segments(nodes: dict, topology: list) -> list[dict]:
    """Return stable segment descriptions while preserving node/interface IDs."""
    if not isinstance(nodes, dict) or not 1 <= len(nodes) <= MAX_NODES:
        raise BootstrapPlanError(f"Topology must contain 1–{MAX_NODES} nodes.")
    if not isinstance(topology, list) or len(topology) > MAX_LINKS:
        raise BootstrapPlanError(f"Topology must contain at most {MAX_LINKS} link records.")

    clean_nodes = {}
    for raw_id, raw_node in nodes.items():
        node_id = str(raw_id)
        if not _SAFE_ID.fullmatch(node_id) or not isinstance(raw_node, dict):
            raise BootstrapPlanError("Topology contains an invalid node record.")
        if _SENSITIVE_METADATA.search(node_id):
            raise BootstrapPlanError("A node ID contains credential-like text and cannot be exported.")
        if any(
            raw_node.get(field) is not None and not isinstance(raw_node.get(field), (str, int))
            for field in ("name", "template", "image", "image_build")
        ):
            raise BootstrapPlanError("Topology contains invalid node image metadata.")
        if any(
            isinstance(raw_node.get(field), str) and _SENSITIVE_METADATA.search(raw_node[field])
            for field in ("name", "template", "image", "image_build")
        ):
            raise BootstrapPlanError("Node metadata contains credential-like text and cannot be exported.")
        clean_nodes[node_id] = raw_node
    known = set(clean_nodes)
    networks: dict[str, list[dict]] = {}
    segments = []

    for index, row in enumerate(topology):
        if not isinstance(row, dict):
            raise BootstrapPlanError("Topology contains an invalid link record.")
        source_type, destination_type = row.get("source_type"), row.get("destination_type")
        if source_type is not None and not isinstance(source_type, str):
            raise BootstrapPlanError("Topology contains an invalid source type.")
        if destination_type is not None and not isinstance(destination_type, str):
            raise BootstrapPlanError("Topology contains an invalid destination type.")
        if source_type == "node" and destination_type == "node":
            left = _endpoint(_node_id(row.get("source"), known), row.get("source_label"), clean_nodes)
            right = _endpoint(_node_id(row.get("destination"), known), row.get("destination_label"), clean_nodes)
            segments.append({"id": f"cable:{index}", "kind": "direct", "endpoints": [left, right]})
        elif source_type == "node" and destination_type == "network":
            network_id = "" if row.get("destination") is None else str(row["destination"])
            if not _SAFE_ID.fullmatch(network_id):
                raise BootstrapPlanError("Topology contains an invalid network ID.")
            if _SENSITIVE_METADATA.search(network_id):
                raise BootstrapPlanError("A network ID contains credential-like text and cannot be exported.")
            endpoint = _endpoint(_node_id(row.get("source"), known), row.get("source_label"), clean_nodes)
            networks.setdefault(network_id, []).append(endpoint)
        elif destination_type == "node" and source_type == "network":
            network_id = "" if row.get("source") is None else str(row["source"])
            if not _SAFE_ID.fullmatch(network_id):
                raise BootstrapPlanError("Topology contains an invalid network ID.")
            if _SENSITIVE_METADATA.search(network_id):
                raise BootstrapPlanError("A network ID contains credential-like text and cannot be exported.")
            endpoint = _endpoint(_node_id(row.get("destination"), known), row.get("destination_label"), clean_nodes)
            networks.setdefault(network_id, []).append(endpoint)
        else:
            raise BootstrapPlanError("Topology contains an unsupported node/link relationship.")

    for network_id, endpoints in networks.items():
        segments.append({"id": f"network:{network_id}", "kind": "network", "endpoints": endpoints})

    seen_all_ports = set()
    for segment in segments:
        endpoints = segment["endpoints"]
        if len(endpoints) < 2 and segment["kind"] == "direct":
            raise BootstrapPlanError("A direct cable must have exactly two endpoints.")
        seen_nodes = set()
        seen_ports = set()
        for endpoint in endpoints:
            label = endpoint["interface"] or endpoint["interface_label"]
            port = (endpoint["node_id"], label.casefold()) if label else None
            if endpoint["node_id"] in seen_nodes:
                raise BootstrapPlanError(
                    f"Segment {segment['id']} connects a node more than once; interface addressing would be ambiguous."
                )
            if port is not None and port in seen_ports:
                raise BootstrapPlanError(f"Segment {segment['id']} repeats an interface.")
            if port is not None and port in seen_all_ports:
                raise BootstrapPlanError(
                    f"Interface {label} on node {endpoint['node_id']} appears in multiple segments."
                )
            seen_nodes.add(endpoint["node_id"])
            if port is not None:
                seen_ports.add(port)
                seen_all_ports.add(port)
        endpoints.sort(key=lambda item: (_node_sort(item["node_id"]), (item["interface"] or item["interface_label"]).casefold()))

    segments.sort(key=lambda item: item["id"])
    return segments


def _prefix_for_host_count(count: int) -> int:
    for prefix in range(30, 0, -1):
        if (1 << (32 - prefix)) - 2 >= count:
            return prefix
    raise BootstrapPlanError("A segment contains too many endpoints for IPv4 subnetting.")


def _available_hosts(
    network: ipaddress.IPv4Network,
    reserved: set[ipaddress.IPv4Address],
    count: int,
):
    available = []
    for address in network.hosts():
        if address in reserved:
            continue
        available.append(address)
        if len(available) == count:
            break
    return available


def _fingerprint(
    nodes: dict,
    topology: list,
    management_pool: str,
    data_pool: str,
    reserved_networks=(),
    reserved_ips=(),
) -> str:
    safe_input = {
        "nodes": nodes,
        "topology": topology,
        "management_pool": management_pool,
        "data_pool": data_pool,
        "reserved_networks": sorted(str(value) for value in reserved_networks),
        "reserved_ips": sorted(str(value) for value in reserved_ips),
    }
    encoded = json.dumps(safe_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _allocate_subnet(
    pool: ipaddress.IPv4Network,
    prefix: int,
    occupied: list[ipaddress.IPv4Network],
) -> ipaddress.IPv4Network:
    if prefix < pool.prefixlen:
        raise BootstrapPlanError(f"Data pool {pool} is too small for a /{prefix} segment.")
    size = 1 << (32 - prefix)
    pool_end = int(pool.broadcast_address)
    candidate_start = int(pool.network_address)
    occupied = sorted(occupied, key=lambda item: int(item.network_address))
    while candidate_start + size - 1 <= pool_end:
        candidate = ipaddress.ip_network((candidate_start, prefix))
        collision = next((item for item in occupied if candidate.overlaps(item)), None)
        if collision is None:
            return candidate
        candidate_start = ((int(collision.broadcast_address) // size) + 1) * size
    raise BootstrapPlanError(f"Data pool {pool} has no free /{prefix} subnet remaining.")


def build_plan(
    nodes: dict,
    topology: list,
    *,
    management_segment_id: str | None,
    management_pool: str,
    data_pool: str = DEFAULT_DATA_POOL,
    reserved_ips=(),
    reserved_networks=(),
) -> dict:
    """Build an IP-only plan; per-segment gateways are not represented or inferred.

    The request contract intentionally has no gateway parameter. A scalar
    gateway is ambiguous for a topology with multiple networks, while this
    model has no verified per-segment gateway/reachability source.
    """
    try:
        nodes, topology = normalize_topology_data({"nodes": nodes, "topology": topology})
    except TopologyDataError as exc:
        raise BootstrapPlanError(str(exc)) from exc
    segments = inspect_segments(nodes, topology)
    management = _ipv4_network(management_pool, "Management pool")
    data = _ipv4_network(data_pool, "Data pool")
    if management.overlaps(data):
        raise BootstrapPlanError("Management and data pools overlap; choose disjoint IPv4 ranges.")

    reserved = set()
    try:
        reserved_ip_values = iter(reserved_ips)
    except TypeError as exc:
        raise BootstrapPlanError("Existing project addresses must be an iterable of IPv4 addresses.") from exc
    for raw in reserved_ip_values:
        try:
            address = ipaddress.ip_address(str(raw))
        except ValueError as exc:
            raise BootstrapPlanError("An existing project address is invalid.") from exc
        if not isinstance(address, ipaddress.IPv4Address):
            raise BootstrapPlanError("Existing project addresses must be IPv4.")
        reserved.add(address)
    reserved_subnets = []
    try:
        reserved_network_values = iter(reserved_networks)
    except TypeError as exc:
        raise BootstrapPlanError("Existing project subnets must be an iterable of IPv4 CIDRs.") from exc
    for raw in reserved_network_values:
        try:
            network = ipaddress.ip_network(str(raw), strict=True)
        except ValueError as exc:
            raise BootstrapPlanError("An existing project subnet is invalid.") from exc
        if not isinstance(network, ipaddress.IPv4Network):
            raise BootstrapPlanError("An existing project subnet is not IPv4.")
        reserved_subnets.append(network)

    if any(network.overlaps(management) and network != management for network in reserved_subnets):
        raise BootstrapPlanError("Management pool overlaps an existing project subnet.")

    candidates = [segment for segment in segments if segment["kind"] == "network"]
    candidate_ids = {segment["id"] for segment in candidates}
    topology_fingerprint = hashlib.sha256(json.dumps(
        {"nodes": nodes, "topology": topology},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    if not management_segment_id:
        if not candidates and any(segment["kind"] == "direct" for segment in segments):
            management_issue = (
                "No PNETLab network segment was discovered. Direct node-to-node cables are not "
                "management candidates. Connect a node to a PNETLab network reachable from the "
                "local agent, then rediscover."
            )
        elif not candidates:
            management_issue = (
                "No PNETLab network segment was discovered. Connect a node to a PNETLab network "
                "reachable from the local agent, then rediscover."
            )
        else:
            management_issue = (
                "Select the PNETLab network that is actually reachable from the local agent as "
                "the management segment."
            )
        return {
            "status": "NEEDS_MANUAL_ACTION",
            "ready_to_import": False,
            "topology_sha256": topology_fingerprint,
            "management_pool": str(management),
            "data_pool": str(data),
            "gateway_status": "NOT_SUPPORTED",
            "segments": segments,
            "management_segment_candidates": [segment["id"] for segment in candidates],
            "issues": [management_issue],
            "nodes": [],
            "configuration": {
                "status": "NEEDS_MANUAL_ACTION",
                "reason": "Exact VM image profiles and image-specific startup configuration have not been verified.",
            },
        }

    selected_id = str(management_segment_id)
    if selected_id not in candidate_ids:
        raise BootstrapPlanError("The selected management segment is not a discovered PNETLab network.")
    selected = next(segment for segment in candidates if segment["id"] == selected_id)
    management_endpoints = [endpoint for endpoint in selected["endpoints"] if endpoint["ip_assignable"]]
    if not management_endpoints:
        raise BootstrapPlanError("The selected management segment has no recognized Layer-3 endpoint.")
    management_hosts = _available_hosts(management, reserved, len(management_endpoints))
    if len(management_hosts) < len(management_endpoints):
        raise BootstrapPlanError(f"Management pool {management} does not have enough unreserved host addresses.")

    assignments: dict[str, list[dict]] = {str(node_id): [] for node_id in nodes}
    occupied = [management, *reserved_subnets]
    for segment in segments:
        if segment["id"] == selected_id:
            subnet = management
            pool_kind = "management"
            addressed_endpoints = management_endpoints
        elif len([endpoint for endpoint in segment["endpoints"] if endpoint["ip_assignable"]]) >= 2:
            addressed_endpoints = [endpoint for endpoint in segment["endpoints"] if endpoint["ip_assignable"]]
            prefix = _prefix_for_host_count(len(addressed_endpoints))
            subnet = _allocate_subnet(data, prefix, occupied)
            occupied.append(subnet)
            pool_kind = "data"
        else:
            # A one-node external/cloud attachment has an unknown peer subnet;
            # assigning its IP here would require assumptions about the host.
            segment["note"] = (
                "Fewer than two recognized Layer-3 endpoints; no data subnet was assigned. "
                "Unknown endpoints and Layer-2 switch ports are not counted as hosts."
            )
            continue

        available = _available_hosts(subnet, reserved, len(addressed_endpoints))
        if len(available) < len(addressed_endpoints):
            raise BootstrapPlanError(f"Subnet {subnet} does not have enough unreserved host addresses.")
        segment["subnet"] = str(subnet)
        segment["addressing"] = pool_kind
        for endpoint, address in zip(addressed_endpoints, available):
            assignments[endpoint["node_id"]].append({
                "segment_id": segment["id"],
                "interface": endpoint["interface"],
                "ip": str(address),
                "prefix_length": subnet.prefixlen,
                "subnet": str(subnet),
                "addressing": pool_kind,
                "gateway": None,
            })

    output_nodes = []
    for node_id in sorted(assignments, key=_node_sort):
        node = nodes[node_id]
        template = str(node.get("template") or "")
        output_nodes.append({
            "node_id": node_id,
            "name": str(node.get("name") or f"Node {node_id}")[:128],
            "template": template[:128],
            "image": str(node.get("image") or "")[:512] or None,
            "image_build": str(node.get("image_build") or "")[:128] or None,
            "addresses": assignments[node_id],
            "configuration_status": "NEEDS_MANUAL_ACTION",
            "configuration_reason": (
                "No exact image build and startup-config profile has been verified; no device config was generated."
                if assignments[node_id]
                else "No recognized Layer-3 interface received an address; SVI, host and unknown-image handling needs review."
            ),
            "ssh_status": "NEEDS_MANUAL_ACTION",
            "ssh_reason": "SSH credentials, host-key setup and image-specific key generation have not been verified.",
        })

    return {
        "status": "PLANNED",
        "ready_to_import": False,
        "plan_id": _fingerprint(
            nodes, topology, str(management), str(data) + "|" + selected_id,
            reserved_subnets, reserved,
        ),
        "topology_sha256": topology_fingerprint,
        "next_action": "NEEDS_MANUAL_ACTION",
        "management_pool": str(management),
        "data_pool": str(data),
        "gateway_status": "NOT_SUPPORTED",
        "management_segment_id": selected_id,
        "segments": segments,
        "nodes": output_nodes,
        "manifest": {
            "schema_version": "1.0",
            "plan_id": _fingerprint(
                nodes, topology, str(management), str(data) + "|" + selected_id,
                reserved_subnets, reserved,
            ),
            "topology_sha256": topology_fingerprint,
            "management_segment_id": selected_id,
            "management_pool": str(management),
            "data_pool": str(data),
            "gateway_status": "NOT_SUPPORTED",
            "nodes": [
                {
                    "node_id": node["node_id"],
                    "name": node["name"],
                    "template": node["template"],
                    "image": node["image"],
                    "image_build": node["image_build"],
                    "addresses": node["addresses"],
                }
                for node in output_nodes
            ],
            "segments": [
                {
                    "id": segment["id"],
                    "kind": segment["kind"],
                    "subnet": segment.get("subnet"),
                    "endpoints": [
                        {"node_id": endpoint["node_id"], "interface": endpoint["interface"]}
                        for endpoint in segment["endpoints"]
                    ],
                }
                for segment in segments
            ],
            "configuration_artifacts": [],
            "status": "PLANNED",
        },
        "routing_status": "NOT_PLANNED",
        "warnings": [
            "The management segment must be verified as reachable from the local agent; topology membership alone does not prove reachability.",
            "Existing running-config interface addresses are not discovered; reconcile this plan against device configuration before using it.",
            "Layer-2 switch physical ports are not assigned host IPs; switch management needs an explicit SVI/VLAN plan.",
            "Gateway inputs are not supported; no per-segment gateway or routing configuration was validated or inferred.",
            "No startup config, PNETLab write, SSH connection or inventory update was performed.",
        ],
        "configuration": {
            "status": "NEEDS_MANUAL_ACTION",
            "reason": "The discovery response does not establish exact VM image builds; image-specific startup configuration is disabled until profiles are verified.",
        },
    }
