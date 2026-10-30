"""Push a project's topology to PNETLab as a new lab (Sơ đồ mạng -> Đẩy sang PNETLab).

The reverse of pnetlab_import: every device becomes a node of the matching
PNETLab template at its canvas position, every cable a point-to-point link,
a Cloud/Internet device or the management side of each managed device a
Cloud0 (pnet0) network, and a shared segment a bridge network.

It only ever creates a *new* lab under a folder of its own; a lab name that
is taken is refused, and a build that fails part way deletes what it made.
Nodes are created stopped and unconfigured: the console bootstrap (Thiết bị
-> Khởi tạo) gives them a management address and SSH afterwards.
"""

import logging
import re

from flask import current_app
from pydantic import BaseModel, ConfigDict, Field

from ..devices.model import Device
from ..errors import AppError
from ..extensions import db
from ..integrations.pnetlab import PNETLabClient, PNETLabError
from ..integrations.pnetlab_writer import ExistingLabError, PNETLabWriter
from ..projects.service import validate
from .interfaces import normalize_label
from .model import TopologyLink

LAB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
FOLDER = re.compile(r"^/?[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}(/[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}){0,3}$")
CLOUD_NAME = "Cloud0"

logger = logging.getLogger(__name__)


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    verify_tls: bool = True
    # Accepted (the saved login carries them) but not used by an export.
    lab: str = Field(default="", max_length=255)
    api_mode: str = Field(default="session", max_length=16)
    folder: str = Field(default="/NetAI", max_length=255)
    lab_name: str = Field(min_length=1, max_length=64)
    # Cable each managed device's management port to Cloud0.
    management_cloud: bool = True


# -- what each NetAI device becomes ----------------------------------------------

def _template_for(device) -> tuple[str, str] | None:
    """("node", template) or ("network", pnetlab network type); None: not exported."""
    if device.device_type == "paloalto_panos":
        return "node", "paloalto"
    if device.device_type == "cisco_asa":
        return "node", "asav"
    if device.kind in ("switch", "l3_switch"):
        return "node", "viosl2"
    if device.device_type == "cisco_ios":
        return "node", "vios"
    if device.kind in ("pc", "server"):
        return "node", "vpcs"
    if device.kind == "cloud":
        return "network", "pnet0"
    if device.kind == "segment":
        return "network", "bridge"
    return None


def interface_index(template: str, name: str | None) -> int | None:
    """PNETLab's interface index for a NetAI interface name on a template.

    vIOS numbers its ports Gi0/0..Gi0/n; IOSv-L2 has four per slot (Gi1/0 is
    4); ASAv and PA-VM keep index 0 for their management port; VPCS has one.
    The node's own interface names, read back after it is added, win over
    this when PNETLab reports them.
    """
    canonical = normalize_label(name) or (name or "").strip()
    text = canonical.lower()
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    if template == "vpcs":
        return 0
    if template == "asav":
        if text.startswith("management"):
            return 0
        return numbers[-1] + 1 if numbers else None
    if template == "paloalto":
        if text in ("mgmt", "management"):
            return 0
        return numbers[-1] if len(numbers) >= 2 else None
    if template == "viosl2":
        return numbers[-2] * 4 + numbers[-1] if len(numbers) >= 2 else None
    return numbers[-1] if numbers else None


def _position(device, order: int) -> tuple[int, int]:
    if device.pos_x is not None and device.pos_y is not None:
        return max(int(device.pos_x), 0), max(int(device.pos_y), 0)
    return 120 + (order % 6) * 160, 120 + (order // 6) * 140


# -- the plan ------------------------------------------------------------------------

def plan(project, request: ExportRequest, available: set[str] | None = None) -> dict:
    """Nodes, networks, cables and warnings for the project; touches nothing."""
    devices = (
        db.session.query(Device).filter(Device.project_id == project.id)
        .order_by(Device.hostname, Device.id).all()
    )
    links = (
        db.session.query(TopologyLink).filter(TopologyLink.project_id == project.id)
        .order_by(TopologyLink.id).all()
    )
    warnings: list[str] = []
    nodes, networks, by_device = [], [], {}
    for order, device in enumerate(devices):
        mapping = _template_for(device)
        left, top = _position(device, order)
        if mapping is None:
            warnings.append(f"{device.hostname}: no PNETLab equivalent for a {device.kind}; left out.")
            continue
        what, template = mapping
        if what == "network":
            entry = {"device_id": device.id, "hostname": device.hostname, "type": template,
                     "name": CLOUD_NAME if template == "pnet0" else device.hostname,
                     "left": left, "top": top}
            # Every Cloud/Internet device shares the one Cloud0 network.
            if template == "pnet0" and any(n["type"] == "pnet0" for n in networks):
                entry = next(n for n in networks if n["type"] == "pnet0")
            else:
                networks.append(entry)
            by_device[device.id] = ("network", entry)
            continue
        ready = available is None or template in available
        if not ready:
            warnings.append(f"{device.hostname}: template {template} is not available on this PNETLab; left out.")
        entry = {"device_id": device.id, "hostname": device.hostname, "template": template,
                 "left": left, "top": top, "ready": ready, "used": set(), "managed": bool(device.managed),
                 "management_ip": device.management_ip}
        nodes.append(entry)
        by_device[device.id] = ("node", entry)

    cables = []
    for link in links:
        a, b = by_device.get(link.device_a_id), by_device.get(link.device_b_id)
        label = f"{_hostname(devices, link.device_a_id)} {link.interface_a} - {_hostname(devices, link.device_b_id)} {link.interface_b}"
        if a is None or b is None or (a[0] == "node" and not a[1]["ready"]) or (b[0] == "node" and not b[1]["ready"]):
            warnings.append(f"{label}: an end is not exported; cable left out.")
            continue
        if a[0] == "network" and b[0] == "network":
            warnings.append(f"{label}: two networks cannot be cabled together; left out.")
            continue
        ends = []
        for (kind, entry), interface in ((a, link.interface_a), (b, link.interface_b)):
            if kind == "network":
                ends.append({"network": entry["name"], "hostname": entry["hostname"]})
                continue
            index = interface_index(entry["template"], interface)
            if index is None or index in entry["used"]:
                ends.append(None)
                break
            entry["used"].add(index)
            ends.append({"hostname": entry["hostname"], "interface": interface, "index": index})
        if None in ends:
            warnings.append(f"{label}: interface name not understood or used twice; left out.")
            continue
        cables.append({"label": label, "a": ends[0], "b": ends[1]})

    management = []
    if request.management_cloud:
        for entry in nodes:
            if not (entry["ready"] and entry["managed"] and entry["management_ip"]):
                continue
            index = next(i for i in range(64) if i not in entry["used"])
            entry["used"].add(index)
            management.append({"hostname": entry["hostname"], "index": index})
        if management and not any(n["type"] == "pnet0" for n in networks):
            networks.insert(0, {"device_id": None, "hostname": None, "type": "pnet0",
                                "name": CLOUD_NAME, "left": 60, "top": 40})

    for entry in nodes:
        entry["ethernet"] = max([4, *[i + 1 for i in entry["used"]]])
        entry["used"] = sorted(entry["used"])
    folder = "/" + request.folder.strip("/")
    return {
        "lab_path": f"{folder.rstrip('/')}/{request.lab_name}.unl",
        "nodes": nodes, "networks": networks, "cables": cables,
        "management": management, "warnings": warnings,
    }


def _hostname(devices, device_id) -> str:
    return next((d.hostname for d in devices if d.id == device_id), f"#{device_id}")


def _check(request: ExportRequest) -> None:
    from ..errors import ValidationError

    if not LAB_NAME.match(request.lab_name):
        raise ValidationError("Lab name: letters, digits, space, '.', '_' or '-' (max 64).",
                              {"lab_name": ["Invalid."]})
    if not FOLDER.match(request.folder):
        raise ValidationError("Folder: up to four names such as /NetAI/CSN301.",
                              {"folder": ["Invalid."]})


def _writer(request: ExportRequest) -> PNETLabWriter:
    factory = current_app.config.get("PNETLAB_WRITER_FACTORY")
    if factory is not None:
        return factory(request)
    return PNETLabWriter(PNETLabClient(request.url, request.username, request.password,
                                       verify_tls=request.verify_tls, mode="session"))


def validate_request(payload: dict) -> ExportRequest:
    request = validate(ExportRequest, payload, "Export")
    _check(request)
    return request


# -- preview and run ------------------------------------------------------------------

def preview(project, payload: dict) -> dict:
    """The plan, checked against the templates this PNETLab has. Builds nothing."""
    request = validate_request(payload)
    writer = _writer(request)
    try:
        writer.login()
        available = set(writer.templates())
        exists = writer.lab_exists("/" + request.folder.strip("/"), request.lab_name)
    except AppError:
        raise
    except Exception as exc:  # an answer shape PNETLab was not expected to give
        logger.exception("PNETLab export preview failed")
        raise PNETLabError(
            f"PNETLab trả lời không như mong đợi ({type(exc).__name__}); xem log máy chủ."
        ) from exc
    result = plan(project, request, available)
    if exists:
        result["warnings"].insert(0, f"{result['lab_path']} already exists; choose another lab name.")
        result["blocked"] = True
    return result


def _node_body(template: str, options: dict, entry: dict) -> dict:
    def default(key, fallback):
        option = options.get(key)
        return option.get("value", fallback) if isinstance(option, dict) else fallback

    body = {
        "template": template, "type": default("type", "qemu"), "image": default("image", ""),
        "name": entry["hostname"], "icon": default("icon", "Router.png"),
        "cpulimit": 1, "cpu": default("cpu", 1), "ram": default("ram", 1024),
        "ethernet": max(int(default("ethernet", 4) or 4), entry["ethernet"]),
        "console": default("console", "telnet"), "config": 0, "delay": 0,
        "left": entry["left"], "top": entry["top"], "count": "1", "postfix": 0,
    }
    for key in ("qemu_arch", "qemu_nic", "qemu_version", "qemu_options", "config_script"):
        value = default(key, None)
        if value not in (None, ""):
            body[key] = value
    if not body["image"]:
        body.pop("image")
    return body


def _actual_index(topology: dict, node_id: str, wanted: int, name: str | None) -> int:
    """The index PNETLab reports for ``name`` on the node, else ``wanted``."""
    node = (topology.get("nodes") or {}).get(str(node_id)) or {}
    ethernets = node.get("ethernets") or {}
    items = ethernets.items() if isinstance(ethernets, dict) else enumerate(ethernets)
    target = (normalize_label(name) or name or "").lower()
    for index, eth in items:
        label = eth.get("name") if isinstance(eth, dict) else None
        if label and target and (normalize_label(label) or label).lower() == target:
            return int(index)
    return wanted


def run(project, payload: dict, progress=None) -> dict:
    """Build the lab; on failure delete it again. Returns per-device results."""
    progress = progress or (lambda **kw: None)
    request = validate_request(payload)
    writer = _writer(request)
    writer.login()
    available = set(writer.templates())
    built = plan(project, request, available)
    folder = "/" + request.folder.strip("/")
    results = [{"hostname": n["hostname"], "template": n["template"],
                "status": "pending" if n["ready"] else "skipped"} for n in built["nodes"]]

    progress(phase="lab", message=f"Creating {built['lab_path']}")
    path = writer.create_lab(folder, request.lab_name)  # ExistingLabError: nothing to undo
    try:
        options = {t: writer.template_options(t) for t in {n["template"] for n in built["nodes"] if n["ready"]}}
        node_ids = {}
        for entry, result in zip(built["nodes"], results):
            if not entry["ready"]:
                continue
            progress(hostname=entry["hostname"], phase="node", message=f"Adding {entry['template']}")
            node_ids[entry["hostname"]] = writer.add_node(_node_body(entry["template"], options[entry["template"]], entry))
            result.update(status="created", node_id=node_ids[entry["hostname"]])

        network_ids = {}
        for network in built["networks"]:
            progress(phase="network", message=f"Adding network {network['name']}")
            network_ids[network["name"]] = writer.add_network(
                network["type"], network["name"], network["left"], network["top"],
                "cloud.png" if network["type"] == "pnet0" else "lan.png")

        topology = writer.topology()
        cabled = 0
        for cable in built["cables"]:
            progress(phase="cable", message=cable["label"])
            a, b = cable["a"], cable["b"]
            if "network" in a or "network" in b:
                node, net = (b, a) if "network" in a else (a, b)
                index = _actual_index(topology, node_ids[node["hostname"]], node["index"], node["interface"])
                writer.attach(node_ids[node["hostname"]], index, network_ids[net["network"]])
            else:
                writer.connect(
                    node_ids[a["hostname"]], _actual_index(topology, node_ids[a["hostname"]], a["index"], a["interface"]),
                    node_ids[b["hostname"]], _actual_index(topology, node_ids[b["hostname"]], b["index"], b["interface"]))
            cabled += 1
        for item in built["management"]:
            progress(hostname=item["hostname"], phase="management", message="Cabling to Cloud0")
            writer.attach(node_ids[item["hostname"]], item["index"], network_ids[CLOUD_NAME])
            result = next(r for r in results if r["hostname"] == item["hostname"])
            result["management_index"] = item["index"]
    except Exception as exc:
        progress(phase="rollback", message=f"Deleting {path}")
        writer.delete_lab(path)
        message = exc.message if isinstance(exc, PNETLabError) else type(exc).__name__
        raise PNETLabError(f"{message} The half-built lab {path} was deleted.") from exc

    return {
        "devices": results, "api": "session",
        "summary": {"lab_path": path, "cables": cabled, "networks": len(network_ids),
                    "management": len(built["management"]), "warnings": built["warnings"]},
    }


__all__ = ["ExistingLabError", "interface_index", "plan", "preview", "run", "validate_request"]
