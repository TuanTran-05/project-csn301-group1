"""Undo for device deletion.

Deleting a device can hand back a restore token: the device row, its SSH
login (still encrypted), its cables, its per-device design and its group
memberships, sealed with the credential key so a client can neither read nor
alter it. Restoring the token recreates those rows. Change history, backups
and monitoring snapshots are deleted for good and do not come back.
"""

import json
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from ..errors import ConflictError, ValidationError
from ..extensions import db
from .model import Device

# A token is only accepted for this long after the deletion.
RESTORE_TTL_SECONDS = 24 * 60 * 60


def _tables():
    from ..credentials.model import DeviceCredential
    from ..groups.model import group_devices
    from ..topology.model import (
        DesignAccessPort,
        DesignLoopback,
        DesignOspf,
        DesignStaticRoute,
        DesignSubif,
        DesignSvi,
    )

    per_device = [
        model.__table__
        for model in (
            DeviceCredential,
            DesignAccessPort,
            DesignSvi,
            DesignSubif,
            DesignLoopback,
            DesignStaticRoute,
            DesignOspf,
        )
    ]
    return per_device + [group_devices]


def _fernet() -> Fernet | None:
    key = current_app.config.get("CREDENTIAL_ENCRYPTION_KEY")
    if not key:
        current_app.logger.warning(
            "Device deletion undo is unavailable because CREDENTIAL_ENCRYPTION_KEY is not configured."
        )
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (TypeError, ValueError):
        current_app.logger.warning(
            "Device deletion undo is unavailable because CREDENTIAL_ENCRYPTION_KEY is invalid."
        )
        return None


def _dump(row) -> dict:
    return {
        key: {"__dt__": value.isoformat()} if isinstance(value, datetime) else value
        for key, value in row._mapping.items()
    }


def _load(values: dict) -> dict:
    return {
        key: datetime.fromisoformat(value["__dt__"]) if isinstance(value, dict) and "__dt__" in value else value
        for key, value in values.items()
    }


def _select(table, column, device_id):
    return [_dump(row) for row in db.session.execute(table.select().where(column == device_id))]


def snapshot(device: Device) -> str | None:
    """A sealed restore token, or None when the server has no usable key."""
    fernet = _fernet()
    if fernet is None:
        return None
    from ..topology.model import TopologyLink

    links = TopologyLink.__table__
    document = {
        "project_id": device.project_id,
        "device": _select(Device.__table__, Device.__table__.c.id, device.id)[0],
        "rows": {table.name: _select(table, table.c.device_id, device.id) for table in _tables()},
        "links": [
            _dump(row)
            for row in db.session.execute(
                links.select().where(db.or_(links.c.device_a_id == device.id, links.c.device_b_id == device.id))
            )
        ],
    }
    return fernet.encrypt(json.dumps(document).encode()).decode()


def _open(token) -> dict:
    if not isinstance(token, str) or not token:
        raise ValidationError("restore_token is required.")
    fernet = _fernet()
    if fernet is None:
        raise ValidationError("Undo is unavailable because the server has no valid encryption key.")
    try:
        return json.loads(fernet.decrypt(token.encode(), ttl=RESTORE_TTL_SECONDS))
    except InvalidToken as exc:
        raise ValidationError("This deletion can no longer be undone.") from exc


def restore(token, project) -> Device:
    """Recreate the device sealed in ``token`` inside ``project``."""
    from ..topology.model import TopologyLink

    document = _open(token)
    if document["project_id"] != project.id:
        raise ValidationError("This deletion belongs to another project.")
    values = _load(document["device"])
    clash = db.session.query(Device).filter(
        Device.project_id == project.id,
        db.or_(
            Device.hostname == values["hostname"],
            db.and_(Device.management_ip.isnot(None), Device.management_ip == values["management_ip"]),
        ),
    ).first()
    if clash is not None:
        raise ConflictError(
            f"{clash.hostname} now uses that hostname or management IP; rename it before undoing."
        )
    # Keep the old id when it is still free, so cables and earlier undo steps line up.
    old_id = values["id"]
    if db.session.get(Device, old_id) is not None:
        values.pop("id")
    device_id = db.session.execute(Device.__table__.insert().values(**values)).inserted_primary_key[0]

    for table in _tables():
        for row in document["rows"].get(table.name, []):
            row = _load(row)
            row["device_id"] = device_id
            if "id" in table.c and table.c.id.primary_key:
                row.pop("id", None)
            db.session.execute(table.insert().values(**row))

    links = TopologyLink.__table__
    for row in document["links"]:
        row = _load(row)
        row.pop("id", None)
        for side in ("a", "b"):
            if row[f"device_{side}_id"] == old_id:
                row[f"device_{side}_id"] = device_id
        ends = [(row["device_a_id"], row["interface_a"]), (row["device_b_id"], row["interface_b"])]
        # The far end may have been deleted or re-cabled since; skip that cable.
        if any(db.session.get(Device, end) is None for end, _ in ends):
            continue
        taken = db.session.execute(
            links.select().where(
                db.or_(
                    *[
                        db.or_(
                            db.and_(links.c.device_a_id == end, links.c.interface_a == port),
                            db.and_(links.c.device_b_id == end, links.c.interface_b == port),
                        )
                        for end, port in ends
                    ]
                )
            )
        ).first()
        if taken is None:
            db.session.execute(links.insert().values(**row))

    db.session.commit()
    db.session.expire_all()
    return db.session.get(Device, device_id)
