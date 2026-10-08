"""Turn a PNETLab lab into devices and links of a project."""

import re

from pydantic import BaseModel, ConfigDict, Field

from ..devices import service as device_service
from ..errors import AppError
from ..extensions import db
from ..integrations.pnetlab import build_client
from ..projects.service import validate
from . import service as topology_service
from .interfaces import normalize_label
from .templates import free_management_ips

class LabSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    lab: str = Field(min_length=1, max_length=255)
    verify_tls: bool = True


class NodeChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=32)
    hostname: str | None = Field(default=None, max_length=64)
    management_ip: str | None = Field(default=None, max_length=45)


class ImportRequest(LabSource):
    nodes: list[NodeChoice] = Field(min_length=1, max_length=500)
    credential: dict | None = None
    import_links: bool = True


def classify(template: str | None) -> dict | None:
    """Device type and role for a PNETLab template, or None when unsupported."""
    name = (template or "").lower()
    if "asa" in name:
        return {"device_type": "cisco_asa", "role": "firewall"}
    if "l2" in name or "switch" in name:
        return {"device_type": "cisco_ios", "role": "access"}
    if any(key in name for key in ("vios", "iol", "csr", "c7200", "c3725", "c3745", "c1710", "router")):
        return {"device_type": "cisco_ios", "role": "core"}
    return None


def hostname_for(name: str | None, node_id) -> str:
    cleaned = re.sub(r"[^A-Z0-9-]+", "-", (name or "").upper()).strip("-")[:64]
    return cleaned or f"NODE{node_id}"


def _coordinate(raw) -> tuple[float, bool] | None:
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    try:
        if text.endswith("%"):
            return float(text[:-1]), True
        return float(text), False
    except ValueError:
        return None


def layout_positions(nodes: dict) -> dict:
    """Canvas coordinates (960x560 space) for every node that has a position."""
    points = {}
    for key, node in nodes.items():
        left, top = _coordinate(node.get("left")), _coordinate(node.get("top"))
        if left and top:
            points[key] = (left, top)
    if not points:
        return {}
    percent = all(l[1] and t[1] for l, t in points.values())
    result = {}
    if percent:
        for key, (left, top) in points.items():
            result[key] = (60 + left[0] / 100 * 840, 50 + top[0] / 100 * 460)
        return result
    max_x = max(l[0] for l, _ in points.values()) or 1
    max_y = max(t[0] for _, t in points.values()) or 1
    scale = min(1.0, 840 / max_x, 460 / max_y)
    for key, (left, top) in points.items():
        result[key] = (60 + left[0] * scale, 50 + top[0] * scale)
    return result


def derive_links(nodes: dict, topology: list) -> list[dict]:
    """Node-to-node cables from the lab topology.

    PNETLab joins nodes through (usually hidden) bridge networks, so entries
    are grouped by network: a network with exactly two nodes is one cable.
    Direct node-to-node entries are honoured too.
    """
    by_network: dict[str, list] = {}
    links = []
    for entry in topology:
        if not isinstance(entry, dict):
            continue
        source, destination = str(entry.get("source", "")), str(entry.get("destination", ""))
        source_type, destination_type = entry.get("source_type"), entry.get("destination_type")
        if source_type == "node" and destination_type == "node":
            ends = [(source, entry.get("source_label")), (destination, entry.get("destination_label"))]
            links.append(ends)
        elif source_type == "node" and destination_type == "network":
            by_network.setdefault(destination, []).append((source, entry.get("source_label")))
        elif destination_type == "node" and source_type == "network":
            by_network.setdefault(source, []).append((destination, entry.get("destination_label")))
    links.extend(ends for ends in by_network.values() if len(ends) == 2)

    known = {f"node{key}" for key in nodes} | {str(key) for key in nodes}
    result = []
    for (node_a, label_a), (node_b, label_b) in links:
        id_a, id_b = node_a.removeprefix("node"), node_b.removeprefix("node")
        if node_a not in known and id_a not in nodes:
            continue
        if node_b not in known and id_b not in nodes:
            continue
        result.append(
            {
                "node_a": id_a,
                "interface_a": normalize_label(label_a),
                "node_b": id_b,
                "interface_b": normalize_label(label_b),
            }
        )
    return result


def _fetch(source: LabSource):
    client = build_client(source.model_dump())
    return client.nodes(source.lab), client.topology(source.lab)


def preview(project, payload: dict) -> dict:
    """What an import would do, without changing anything."""
    source = validate(LabSource, payload, "Import")
    nodes, topology = _fetch(source)
    positions = layout_positions(nodes)
    existing = {
        d.hostname.upper()
        for d in device_service.list_devices(project_id=project.id)
    }
    ordered = sorted(nodes.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else 0)
    importable = [(k, n) for k, n in ordered if classify(n.get("template"))]
    ips = iter(free_management_ips(project, len(importable)))

    items = []
    for key, node in ordered:
        kind = classify(node.get("template"))
        hostname = hostname_for(node.get("name"), key)
        item = {
            "id": str(key),
            "name": node.get("name"),
            "template": node.get("template"),
            "hostname": hostname,
            "supported": kind is not None,
            "exists": hostname.upper() in existing,
            "device_type": kind["device_type"] if kind else None,
            "role": kind["role"] if kind else None,
            "management_ip": next(ips) if kind else None,
        }
        items.append(item)
    return {"nodes": items, "links": derive_links(nodes, topology), "positions": len(positions)}


def import_lab(project, payload: dict) -> dict:
    """Create the chosen nodes as devices (and their cables as links)."""
    request = validate(ImportRequest, payload, "Import")
    nodes, topology = _fetch(request)
    positions = layout_positions(nodes)
    chosen = {choice.id: choice for choice in request.nodes}

    results, by_node = [], {}
    default_ips = iter(free_management_ips(project, len(chosen) + 1))
    for key, choice in chosen.items():
        node = nodes.get(key)
        if node is None:
            results.append({"id": key, "status": "error", "message": "Node not found in the lab."})
            continue
        kind = classify(node.get("template"))
        hostname = choice.hostname or hostname_for(node.get("name"), key)
        if kind is None:
            results.append({"id": key, "hostname": hostname, "status": "skipped",
                            "message": f"Template {node.get('template')!r} is not a Cisco IOS/ASA node."})
            continue
        device_payload = {
            "hostname": hostname,
            "management_ip": choice.management_ip or next(default_ips),
            "environment": "pnetlab",
            "description": f"Imported from PNETLab ({node.get('template')})",
            **kind,
        }
        if key in positions:
            device_payload["pos_x"], device_payload["pos_y"] = (round(v) for v in positions[key])
        if request.credential:
            device_payload["credential"] = request.credential
        existing = _find_device(project, hostname)
        if existing is not None:
            by_node[key] = existing
            results.append({"id": key, "hostname": hostname, "status": "skipped",
                            "message": "A device with this hostname already exists."})
            continue
        try:
            device = device_service.create_device(device_payload, project)
        except AppError as exc:
            db.session.rollback()
            results.append({"id": key, "hostname": hostname, "status": "error",
                            "message": exc.message, "details": exc.details})
            continue
        by_node[key] = device
        results.append({"id": key, "hostname": hostname, "status": "created",
                        "management_ip": device.management_ip})

    link_results = []
    if request.import_links:
        for link in derive_links(nodes, topology):
            a, b = by_node.get(link["node_a"]), by_node.get(link["node_b"])
            if a is None or b is None:
                continue
            try:
                topology_service.create_link(project, {
                    "device_a_id": a.id, "interface_a": link["interface_a"],
                    "device_b_id": b.id, "interface_b": link["interface_b"],
                })
                link_results.append({"a": a.hostname, "b": b.hostname, "status": "created"})
            except (AppError, ValueError) as exc:
                db.session.rollback()
                message = getattr(exc, "message", str(exc))
                link_results.append({"a": a.hostname, "b": b.hostname, "status": "skipped", "message": message})
    return {"devices": results, "links": link_results}


def _find_device(project, hostname: str):
    for device in device_service.list_devices(project_id=project.id):
        if device.hostname.upper() == hostname.upper():
            return device
    return None


