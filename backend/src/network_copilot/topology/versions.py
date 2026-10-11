"""Saved versions of a diagram: snapshot, compare, restore.

A version holds the exported project document, which never contains
credentials. Restoring therefore keeps devices that still exist (matched by
hostname) so their SSH logins survive, creates the ones that are gone, and
replaces links, design and notes wholesale. Devices that are not in the
version stay unless ``prune`` is set. A restore first saves the current state
as an automatic version, so it can itself be undone.
"""

import json

from pydantic import BaseModel, ConfigDict, Field

from ..devices import service as device_service
from ..devices.model import Device
from ..errors import AppError, ConflictError, NotFoundError, ValidationError
from ..extensions import db
from ..projects import exchange
from ..projects.service import validate
from .model import (
    DesignAccessPort, DesignOspf, DesignStaticRoute, DesignSvi, DesignVlan,
    TopologyLink, TopologyNote, TopologyVersion,
)

MAX_VERSIONS = 30
MAX_BYTES = 2 * 1024 * 1024


class VersionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=255)


class RestoreSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prune: bool = False


def list_versions(project_id: int) -> list[TopologyVersion]:
    return (
        db.session.query(TopologyVersion)
        .filter_by(project_id=project_id)
        .order_by(TopologyVersion.id.desc())
        .all()
    )


def get_version(project_id: int, version_id: int) -> TopologyVersion:
    version = db.session.query(TopologyVersion).filter_by(project_id=project_id, id=version_id).first()
    if version is None:
        raise NotFoundError("Version not found.")
    return version


def _store(project, user, name: str, note: str | None, auto: bool) -> TopologyVersion:
    document = exchange.export_project(project)
    text = json.dumps(document, ensure_ascii=False)
    if len(text.encode()) > MAX_BYTES:
        raise ConflictError("The diagram is too large to save as a version.")
    # Automatic versions are housekeeping: drop the oldest ones first.
    existing = list_versions(project.id)
    while len(existing) >= MAX_VERSIONS:
        victim = next((v for v in reversed(existing) if v.auto), existing[-1])
        if not victim.auto:
            raise ConflictError(f"A project keeps at most {MAX_VERSIONS} versions; delete one first.")
        db.session.delete(victim)
        existing.remove(victim)
    version = TopologyVersion(
        project_id=project.id, name=name[:80], note=note, auto=auto,
        created_by=getattr(user, "id", None), document=text,
        device_count=len(document["devices"]), link_count=len(document["links"]),
    )
    db.session.add(version)
    db.session.commit()
    return version


def create_version(project, user, payload: dict) -> TopologyVersion:
    data = validate(VersionSchema, payload, "Version")
    return _store(project, user, data.name, data.note, auto=False)


def delete_version(project, version_id: int) -> None:
    db.session.delete(get_version(project.id, version_id))
    db.session.commit()


def _document(version: TopologyVersion) -> dict:
    return json.loads(version.document)


def _link_key(item: dict) -> str:
    ends = sorted([f"{item['hostname_a']}:{item['interface_a']}", f"{item['hostname_b']}:{item['interface_b']}"])
    return " <-> ".join(ends)


_DEVICE_FIELDS = ("device_type", "role", "kind", "lifecycle", "management_ip", "data_ip", "data_gateway")


def diff(project, version: TopologyVersion) -> dict:
    """What restoring ``version`` would change, seen from the current state."""
    old, new = _document(version), exchange.export_project(project)
    old_devices = {d["hostname"].upper(): d for d in old["devices"]}
    new_devices = {d["hostname"].upper(): d for d in new["devices"]}
    changed = []
    for key in sorted(old_devices.keys() & new_devices.keys()):
        fields = [f for f in _DEVICE_FIELDS if old_devices[key].get(f) != new_devices[key].get(f)]
        if fields:
            changed.append({"hostname": old_devices[key]["hostname"], "fields": fields})
    old_links = {_link_key(l): l for l in old["links"]}
    new_links = {_link_key(l): l for l in new["links"]}
    link_changed = [k for k in old_links.keys() & new_links.keys()
                    if any(old_links[k].get(f) != new_links[k].get(f)
                           for f in ("link_type", "network", "ip_a", "ip_b", "allowed_vlans"))]
    return {
        "devices": {
            "missing_now": sorted(old_devices[k]["hostname"] for k in old_devices.keys() - new_devices.keys()),
            "added_since": sorted(new_devices[k]["hostname"] for k in new_devices.keys() - old_devices.keys()),
            "changed": changed,
        },
        "links": {
            "missing_now": sorted(old_links.keys() - new_links.keys()),
            "added_since": sorted(new_links.keys() - old_links.keys()),
            "changed": sorted(link_changed),
        },
        "design": {
            name: {"then": len(old["design"].get(name, [])), "now": len(new["design"].get(name, []))}
            for name in sorted(set(old["design"]) | set(new["design"]))
        },
        "notes": {"then": len(old.get("notes", [])), "now": len(new.get("notes", []))},
    }


def _clear_cabling(project) -> None:
    for model in (DesignAccessPort, DesignSvi, DesignStaticRoute, DesignOspf):
        db.session.query(model).filter_by(project_id=project.id).delete()
    db.session.query(DesignVlan).filter_by(project_id=project.id).delete()
    db.session.query(TopologyLink).filter_by(project_id=project.id).delete()
    db.session.query(TopologyNote).filter_by(project_id=project.id).delete()
    db.session.commit()


def restore(project, user, version: TopologyVersion, payload: dict) -> dict:
    options = validate(RestoreSchema, payload, "Restore")
    doc = validate(exchange.Document, _document(version), "Version")
    backup = _store(project, user, f"Trước khi khôi phục «{version.name}»", None, auto=True)

    current = {d.hostname.upper(): d for d in device_service.list_devices(project_id=project.id)}
    wanted = {str(item.get("hostname", "")).upper() for item in doc.devices}
    try:
        _clear_cabling(project)
        by_hostname, created = {}, []
        for item in doc.devices:
            key = str(item.get("hostname", "")).upper()
            device = current.get(key)
            if device is not None:
                _update_in_place(device, item)
            else:
                device = device_service.create_device({k: v for k, v in item.items() if k != "credential"}, project)
                created.append(device.hostname)
            by_hostname[key] = device
        db.session.commit()
        pruned = []
        if options.prune:
            extra = [d for k, d in current.items() if k not in wanted]
            pruned = [d.hostname for d in extra]
            device_service.purge_devices([d.id for d in extra])
        exchange.populate_cabling(project, doc, by_hostname)
    except AppError as exc:
        db.session.rollback()
        raise ValidationError(
            f"Restore failed ({exc.message}); the previous state is saved as version #{backup.id}.",
            exc.details,
        ) from exc
    return {"restored": version.id, "backup_id": backup.id, "created": created, "pruned": pruned}


def _update_in_place(device: Device, item: dict) -> None:
    """Fields that describe the design, never the SSH login or live status."""
    for field in ("role", "kind", "description", "data_ip", "data_gateway", "pos_x", "pos_y"):
        if field in item:
            setattr(device, field, item[field])
