"""Move a project in and out: JSON documents, CSV device lists, cloning.

Credentials never leave in an export, and an imported document can only create
things the normal validators accept; a document that fails halfway leaves no
project behind.
"""

import csv
import io


from pydantic import BaseModel, ConfigDict, Field

from ..devices import service as device_service
from ..devices.schemas import DeviceCreateSchema
from ..errors import AppError, ValidationError
from ..extensions import db
from ..topology import service as topology_service
from . import service as project_service
from .model import Project
from ..devices.model import Device

FORMAT = "network-copilot-project"
VERSION = 1
MAX_DEVICES = 500
MAX_LINKS = 2000
MAX_CSV_BYTES = 1024 * 1024
MAX_CSV_ROWS = 1000

DEVICE_COLUMNS = (
    "hostname", "management_ip", "device_type", "role", "environment",
    "ssh_port", "monitoring_enabled", "description",
)


class ProjectDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=255)
    management_network: str
    environment: str = "pnetlab"


class Document(BaseModel):
    model_config = ConfigDict(extra="ignore")

    format: str
    version: int
    project: ProjectDoc
    devices: list[dict] = Field(default_factory=list, max_length=MAX_DEVICES)
    links: list[dict] = Field(default_factory=list, max_length=MAX_LINKS)
    design: dict = Field(default_factory=dict)


# -- export -------------------------------------------------------------------


def export_project(project: Project) -> dict:
    devices = device_service.list_devices(project_id=project.id)
    doc = {
        "format": FORMAT,
        "version": VERSION,
        "project": {
            "name": project.name,
            "description": project.description,
            "management_network": project.management_network,
            "environment": project.environment,
        },
        "devices": [
            {
                "hostname": d.hostname,
                "management_ip": d.management_ip,
                "device_type": d.device_type,
                "role": d.role,
                "environment": d.environment,
                "ssh_port": d.ssh_port,
                "monitoring_enabled": d.monitoring_enabled,
                "description": d.description,
                "pos_x": d.pos_x,
                "pos_y": d.pos_y,
            }
            for d in devices
        ],
        "links": [
            {
                "hostname_a": link.device_a.hostname,
                "interface_a": link.interface_a,
                "hostname_b": link.device_b.hostname,
                "interface_b": link.interface_b,
                "link_type": link.link_type,
                "network": link.network,
                "ip_a": link.ip_a,
                "ip_b": link.ip_b,
                "allowed_vlans": link.allowed_vlans,
                "bring_up": link.bring_up,
                "description": link.description,
                "nameif_a": link.nameif_a,
                "security_a": link.security_a,
                "nameif_b": link.nameif_b,
                "security_b": link.security_b,
            }
            for link in topology_service.list_links(project.id)
        ],
    }
    from ..topology import design

    doc["design"] = design.export_design(project)
    return doc


def devices_csv(project: Project) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(DEVICE_COLUMNS)
    for d in device_service.list_devices(project_id=project.id):
        row = [getattr(d, column) for column in DEVICE_COLUMNS]
        writer.writerow([_csv_safe(value) for value in row])
    return out.getvalue()


def _csv_safe(value):
    """Stop a spreadsheet from running a cell that starts like a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return "" if value is None else value


# -- import -------------------------------------------------------------------


def _unique_name(owner_id: int, wanted: str) -> str:
    taken = {
        row[0]
        for row in db.session.query(Project.name).filter(Project.owner_id == owner_id)
    }
    if wanted not in taken:
        return wanted
    number = 2
    while f"{wanted} ({number})"[:80] in taken:
        number += 1
    return f"{wanted} ({number})"[:80]


def import_project(user, payload: dict, name: str | None = None) -> Project:
    """Create a new project from an exported document."""
    if user.role == "VIEWER":
        raise ValidationError("A read-only account cannot create projects.")
    doc = project_service.validate(Document, payload, "Document")
    if doc.format != FORMAT or doc.version != VERSION:
        raise ValidationError(
            f"Not a {FORMAT} v{VERSION} document.",
            {"format": [f"Expected {FORMAT!r} version {VERSION}."]},
        )

    project = project_service.create_project(
        user,
        {
            "name": _unique_name(user.id, (name or doc.project.name).strip()),
            "description": doc.project.description,
            "management_network": doc.project.management_network,
            "environment": doc.project.environment,
        },
    )
    try:
        by_hostname = {}
        for index, item in enumerate(doc.devices):
            allowed = {k: v for k, v in item.items() if k not in ("credential",)}
            try:
                device = device_service.create_device(allowed, project)
            except AppError as exc:
                raise ValidationError(
                    f"Device #{index + 1} ({item.get('hostname')}) is invalid: {exc.message}",
                    exc.details,
                ) from exc
            by_hostname[device.hostname.upper()] = device

        for index, item in enumerate(doc.links):
            a = by_hostname.get(str(item.get("hostname_a", "")).upper())
            b = by_hostname.get(str(item.get("hostname_b", "")).upper())
            if a is None or b is None:
                raise ValidationError(f"Link #{index + 1} names a device that is not in the document.")
            fields = {
                k: item.get(k)
                for k in ("interface_a", "interface_b", "link_type", "network", "ip_a",
                          "ip_b", "allowed_vlans", "bring_up", "description")
                if item.get(k) is not None
            }
            try:
                link = topology_service.create_link(
                    project, {"device_a_id": a.id, "device_b_id": b.id, **fields}
                )
                extras = {
                    k: item[k]
                    for k in ("nameif_a", "security_a", "nameif_b", "security_b")
                    if item.get(k) is not None
                }
                if extras:
                    topology_service.update_link(project, link.id, extras)
            except AppError as exc:
                raise ValidationError(f"Link #{index + 1} is invalid: {exc.message}", exc.details) from exc

        from ..topology import design

        design.import_design(project, doc.design, {h: d for h, d in by_hostname.items()})
    except AppError:
        db.session.rollback()
        project_service.delete_project(project)
        raise
    return project


def clone_project(user, source: Project, name: str | None = None) -> Project:
    """A copy of the diagram and design, without credentials, history or sharing."""
    document = export_project(source)
    return import_project(user, document, name or f"{source.name} (copy)")


def import_devices_csv(project: Project, text: str) -> list[Device]:
    """Add devices from CSV. Every row is validated first; nothing is created
    unless all of them pass."""
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("The CSV is empty.")
    if len(text.encode()) > MAX_CSV_BYTES:
        raise ValidationError("The CSV is too large (limit 1 MB).")

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = [f.strip() for f in (reader.fieldnames or [])]
    reader.fieldnames = fields
    if "hostname" not in fields or "management_ip" not in fields:
        raise ValidationError("The CSV needs at least the columns hostname and management_ip.")
    unknown = set(fields) - set(DEVICE_COLUMNS) - {"username", "password", "enable_secret"}
    if unknown:
        raise ValidationError("Unknown CSV columns.", {"columns": sorted(unknown)})

    existing_hosts = {d.hostname.upper() for d in device_service.list_devices(project_id=project.id)}
    existing_ips = {d.management_ip for d in device_service.list_devices(project_id=project.id)}
    context = {"network": project.management_network}
    prepared, problems = [], {}
    seen_hosts, seen_ips = set(), set()

    for number, row in enumerate(reader, start=2):
        if number - 1 > MAX_CSV_ROWS:
            raise ValidationError(f"Too many rows (limit {MAX_CSV_ROWS}).")
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        payload = {k: v for k, v in row.items() if k in DEVICE_COLUMNS and v != ""}
        # Only hostname and IP are required in a CSV; the rest has sensible defaults.
        payload.setdefault("device_type", "cisco_ios")
        payload.setdefault("role", "access")
        if "ssh_port" in payload:
            try:
                payload["ssh_port"] = int(payload["ssh_port"])
            except ValueError:
                problems[f"line {number}"] = ["ssh_port must be a number"]
                continue
        if "monitoring_enabled" in payload:
            payload["monitoring_enabled"] = payload["monitoring_enabled"].lower() in ("1", "true", "yes", "y")
        if row.get("username") or row.get("password"):
            payload["credential"] = {
                "username": row.get("username", ""),
                "password": row.get("password", ""),
                "enable_secret": row.get("enable_secret") or None,
            }
        try:
            data = DeviceCreateSchema.model_validate(payload, context=context)
        except Exception as exc:  # pydantic.ValidationError
            messages = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in getattr(exc, "errors", lambda: [])()]
            problems[f"line {number}"] = messages or [str(exc)]
            continue
        host_key = data.hostname.upper()
        if host_key in existing_hosts or host_key in seen_hosts:
            problems[f"line {number}"] = [f"hostname {data.hostname} is already used"]
            continue
        if data.management_ip in existing_ips or data.management_ip in seen_ips:
            problems[f"line {number}"] = [f"management_ip {data.management_ip} is already used"]
            continue
        seen_hosts.add(host_key)
        seen_ips.add(data.management_ip)
        prepared.append(payload)

    if problems:
        raise ValidationError("The CSV has invalid rows; nothing was imported.", problems)
    if not prepared:
        raise ValidationError("The CSV has no device rows.")

    created = []
    try:
        for payload in prepared:
            created.append(device_service.create_device(payload, project))
    except AppError:
        db.session.rollback()
        device_service.purge_devices([d.id for d in created])
        raise
    return created


