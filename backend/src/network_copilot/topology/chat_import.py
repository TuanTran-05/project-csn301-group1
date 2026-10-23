"""Copy a saved AI chat diagram into the editable project topology."""

import ipaddress
import re

from flask import current_app
from pydantic import ValidationError as PydanticValidationError

from ..ai.schemas import NetworkDiagram
from ..chat.model import ChatMessage, ChatSession
from ..devices import service as device_service
from ..devices.model import Device
from ..devices.schemas import DeviceCreateSchema
from ..errors import NotFoundError, ValidationError
from ..extensions import db
from . import ipam
from .model import TopologyLink
from .templates import next_free_interface


_NODE_TYPES = {
    "internet": ("generic", "internet", "cloud", "provisioned"),
    "router": ("cisco_ios", "core", "router", "planned"),
    "firewall": ("cisco_asa", "firewall", "firewall", "planned"),
    "switch": ("cisco_ios", "access", "switch", "planned"),
    "server": ("generic", "server", "server", "provisioned"),
    "host": ("generic", "host", "pc", "provisioned"),
}
_NODE_ORDER = ("internet", "firewall", "router", "switch", "server", "host")
_IMPORT_MARKER_KEY = "_topology_imported_project_ids"


def _hostname(label: str, fallback: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", label.upper()).strip("-") or fallback
    base = base[:64].rstrip("-") or fallback
    candidate = base
    suffix = 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = f"{base[:64 - len(tail)].rstrip('-')}{tail}"
        suffix += 1
    used.add(candidate)
    return candidate


def _device_payload(project, message_id: int, node, hostname: str, position, addresses: list[str]) -> dict:
    device_type, role, kind, lifecycle = _NODE_TYPES[node.kind]
    description_parts = [f"[chat:{message_id}] AI: {node.label}"]
    if len(addresses) > 1:
        description_parts.append("IP bổ sung: " + ", ".join(addresses[1:]))
    return {
        "hostname": hostname,
        "device_type": device_type,
        "role": role,
        "kind": kind,
        "lifecycle": lifecycle,
        "environment": project.environment if project.environment in {
            "pnetlab", "physical", "virtual", "physical_lab", "production"
        } else "pnetlab",
        "data_ip": addresses[0] if addresses else None,
        "description": " · ".join(description_parts)[:255],
        "pos_x": position[0],
        "pos_y": position[1],
    }


def _link_description(link) -> str | None:
    parts = [link.label.strip()] if link.label.strip() else []
    if link.network:
        parts.append(f"Mạng: {link.network}")
    if link.ip_source:
        parts.append(f"IP nguồn: {link.ip_source}")
    if link.ip_target:
        parts.append(f"IP đích: {link.ip_target}")
    return " · ".join(parts)[:255] or None


def get_suggestion(source_project_id: int, message_id: int):
    message = (
        db.session.query(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .filter(
            ChatMessage.id == message_id,
            ChatMessage.role == "assistant",
            ChatSession.project_id == source_project_id,
        )
        .one_or_none()
    )
    if message is None:
        raise NotFoundError("Không tìm thấy gợi ý sơ đồ trong project này.")

    payload = message.payload if isinstance(message.payload, dict) else {}
    try:
        diagram = NetworkDiagram.model_validate(payload.get("diagram"))
    except PydanticValidationError as exc:
        raise ValidationError("Tin nhắn này không có sơ đồ mạng hợp lệ.") from exc
    return message, diagram


def complete_diagram_addresses(project, raw_diagram: dict) -> tuple[dict, int]:
    """Give every suggested device an address while preserving explicit IPs.

    Link endpoint IPs are copied onto their nodes so both the chat renderer and
    the project canvas carry the same address plan. Missing addresses use a
    free /24 from the project's data addressing pool, never its management
    network.
    """
    try:
        diagram = NetworkDiagram.model_validate(raw_diagram)
    except PydanticValidationError as exc:
        raise ValidationError("Tin nhắn này không có sơ đồ mạng hợp lệ.") from exc

    nodes = {node.id: node.model_dump() for node in diagram.nodes}
    links = [link.model_dump() for link in diagram.links]
    used_ips: set[tuple[int, str]] = set()
    added = 0

    def remember(node_id: str, address: str | None) -> None:
        nonlocal added
        if not address:
            return
        interface = ipaddress.ip_interface(address)
        key = (interface.version, str(interface.ip))
        used_ips.add(key)
        addresses = nodes[node_id]["ip_addresses"]
        if address not in addresses and len(addresses) < 4:
            addresses.append(address)
            added += 1

    for node in diagram.nodes:
        for address in node.ip_addresses:
            interface = ipaddress.ip_interface(address)
            used_ips.add((interface.version, str(interface.ip)))

    for link in links:
        remember(link["source"], link.get("ip_source"))
        remember(link["target"], link.get("ip_target"))

    # If a link declares its subnet but omits endpoint addresses, allocate
    # distinct hosts from that subnet and put them on the same diagram nodes.
    try:
        management = ipaddress.ip_network(project.management_network, strict=False)
    except ValueError:
        management = None
    for link in links:
        raw_network = link.get("network")
        if not raw_network:
            continue
        network = ipaddress.ip_network(raw_network, strict=False)
        if network.version != 4 or (management and network.overlaps(management)):
            continue
        for node_key, field in (
            ("source", "ip_source"),
            ("target", "ip_target"),
        ):
            if link.get(field):
                continue
            node = nodes[link[node_key]]
            matching = next(
                (
                    address
                    for address in node["ip_addresses"]
                    if ipaddress.ip_interface(address).ip in network
                ),
                None,
            )
            if matching:
                link[field] = matching
                continue
            host = next(
                (
                    candidate
                    for candidate in network.hosts()
                    if (4, str(candidate)) not in used_ips
                ),
                None,
            )
            if host is None:
                continue
            address = f"{host}/{network.prefixlen}"
            link[field] = address
            remember(link[node_key], address)

    missing = [node for node in diagram.nodes if not nodes[node.id]["ip_addresses"]]
    if missing:
        pool = ipam.pool_for(project)
        prefix = max(24, pool.prefixlen)
        reserved = []
        for link in links:
            if not link.get("network"):
                continue
            network = ipaddress.ip_network(link["network"], strict=False)
            if network.version == 4:
                reserved.append(network)
        subnet = ipam.allocate(project, prefix, reserved=reserved)
        hosts = iter(subnet.hosts())
        for node in missing:
            host = next(
                (
                    candidate
                    for candidate in hosts
                    if (4, str(candidate)) not in used_ips
                ),
                None,
            )
            if host is None:
                raise ValidationError(
                    f"Dải IP {subnet} không còn đủ địa chỉ cho sơ đồ gợi ý."
                )
            remember(node.id, f"{host}/{subnet.prefixlen}")

    completed = NetworkDiagram.model_validate({
        "title": diagram.title,
        "nodes": list(nodes.values()),
        "links": links,
    })
    return completed.model_dump(), added


def _was_imported(project, message) -> bool:
    payload = message.payload if isinstance(message.payload, dict) else {}
    project_ids = payload.get(_IMPORT_MARKER_KEY, [])
    if isinstance(project_ids, list) and project.id in project_ids:
        return True
    marker = f"[chat:{message.id}]"
    return db.session.query(Device.id).filter(
        Device.project_id == project.id,
        Device.description.contains(marker),
    ).first() is not None


def was_imported(project, message) -> bool:
    """Whether this chat diagram has already been linked to this project."""
    return _was_imported(project, message)


def mark_imported(project, message) -> None:
    payload = dict(message.payload) if isinstance(message.payload, dict) else {}
    project_ids = payload.get(_IMPORT_MARKER_KEY, [])
    if not isinstance(project_ids, list):
        project_ids = []
    if project.id not in project_ids:
        payload[_IMPORT_MARKER_KEY] = [*project_ids, project.id]
    message.payload = payload


def import_diagram(project, message_id: int, source_project_id: int | None = None) -> dict:
    """Import one project assistant message atomically and idempotently."""
    message, diagram = get_suggestion(source_project_id or project.id, message_id)

    completed, _ = complete_diagram_addresses(project, diagram.model_dump())
    diagram = NetworkDiagram.model_validate(completed)
    payload = dict(message.payload) if isinstance(message.payload, dict) else {}
    payload["diagram"] = completed
    message.payload = payload

    if _was_imported(project, message):
        mark_imported(project, message)
        db.session.commit()
        return {"created": False, "message_id": message.id, "devices": [], "links": []}

    device_count = db.session.query(Device.id).filter_by(project_id=project.id).count()
    link_count = db.session.query(TopologyLink.id).filter_by(project_id=project.id).count()
    device_limit = current_app.config.get("MAX_DEVICES_PER_PROJECT", 0)
    link_limit = current_app.config.get("MAX_LINKS_PER_PROJECT", 0)
    if device_limit and device_count + len(diagram.nodes) > device_limit:
        raise ValidationError(f"Project chỉ chứa tối đa {device_limit} thiết bị.")
    if link_limit and link_count + len(diagram.links) > link_limit:
        raise ValidationError(f"Project chỉ chứa tối đa {link_limit} đường nối.")

    known_names = {
        name.upper()
        for (name,) in db.session.query(Device.hostname).filter_by(project_id=project.id)
    }
    positions: dict[str, tuple[float, float]] = {}
    rows = [
        [node for node in diagram.nodes if node.kind == kind]
        for kind in _NODE_ORDER
    ]
    y = 100
    for row in (row for row in rows if row):
        for index, node in enumerate(row):
            positions[node.id] = (130 + index * 210, y)
        y += 155

    node_ips: dict[str, list[str]] = {node.id: list(node.ip_addresses) for node in diagram.nodes}
    for link in diagram.links:
        if link.ip_source and link.ip_source not in node_ips[link.source]:
            node_ips[link.source].append(link.ip_source)
        if link.ip_target and link.ip_target not in node_ips[link.target]:
            node_ips[link.target].append(link.ip_target)

    prepared: list[tuple[object, dict]] = []
    try:
        for index, node in enumerate(diagram.nodes, start=1):
            hostname = _hostname(node.label, f"AI-{node.kind.upper()}-{index}", known_names)
            data = device_service._validate(
                DeviceCreateSchema,
                _device_payload(project, message.id, node, hostname, positions[node.id], node_ips[node.id]),
                project,
            )
            device_service._assert_unique(project.id, data.hostname, data.management_ip)
            prepared.append((node, data.model_dump(exclude={"credential"})))

        devices_by_source: dict[str, Device] = {}
        for node, fields in prepared:
            fields["managed"] = fields["device_type"] != "generic"
            device = Device(project_id=project.id, **fields)
            device.status = "unknown"
            db.session.add(device)
            devices_by_source[node.id] = device
        db.session.flush()

        links: list[TopologyLink] = []
        for suggestion in diagram.links:
            device_a = devices_by_source[suggestion.source]
            device_b = devices_by_source[suggestion.target]
            link = TopologyLink(
                project_id=project.id,
                device_a_id=device_a.id,
                interface_a=next_free_interface(project.id, device_a),
                device_b_id=device_b.id,
                interface_b=next_free_interface(project.id, device_b),
                link_type="physical",
                network=suggestion.network,
                ip_a=suggestion.ip_source,
                ip_b=suggestion.ip_target,
                bring_up=True,
                description=_link_description(suggestion),
            )
            db.session.add(link)
            links.append(link)

        mark_imported(project, message)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return {
        "created": True,
        "message_id": message.id,
        "title": diagram.title,
        "devices": [device.to_dict() for device in devices_by_source.values()],
        "links": [link.to_dict() for link in links],
    }
