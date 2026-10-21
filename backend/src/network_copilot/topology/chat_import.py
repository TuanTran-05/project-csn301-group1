"""Copy a saved AI chat diagram into the editable project topology."""

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


def import_diagram(project, message_id: int, source_project_id: int | None = None) -> dict:
    """Import one project assistant message atomically and idempotently."""
    message, diagram = get_suggestion(source_project_id or project.id, message_id)

    marker = f"[chat:{message.id}]"
    already_imported = db.session.query(Device.id).filter(
        Device.project_id == project.id,
        Device.description.contains(marker),
    ).first() is not None
    if already_imported:
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
                bring_up=True,
                description=_link_description(suggestion),
            )
            db.session.add(link)
            links.append(link)

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
