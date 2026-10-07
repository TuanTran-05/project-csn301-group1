from pydantic import ValidationError as PydanticValidationError

from ..errors import ConflictError, NotFoundError, ValidationError
from ..extensions import db
from .model import Device
from .schemas import DeviceCreateSchema, DeviceUpdateSchema


def _format_pydantic_errors(exc: PydanticValidationError) -> dict:
    details: dict[str, list[str]] = {}
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "_root"
        details.setdefault(field, []).append(error["msg"])
    return details


def _validate(schema, payload: dict, project):
    try:
        return schema.model_validate(
            payload, context={"network": project.management_network}
        )
    except PydanticValidationError as exc:
        raise ValidationError(
            "Device payload failed validation.", _format_pydantic_errors(exc)
        ) from exc


def _assert_unique(
    project_id: int,
    hostname: str | None,
    management_ip: str | None,
    exclude_id=None,
):
    """Hostnames and management IPs only have to be unique inside a project."""
    if hostname is not None:
        query = db.session.query(Device).filter(
            Device.project_id == project_id, Device.hostname == hostname
        )
        if exclude_id is not None:
            query = query.filter(Device.id != exclude_id)
        if query.first() is not None:
            raise ConflictError(f"A device named {hostname} already exists.")

    if management_ip is not None:
        query = db.session.query(Device).filter(
            Device.project_id == project_id, Device.management_ip == management_ip
        )
        if exclude_id is not None:
            query = query.filter(Device.id != exclude_id)
        if query.first() is not None:
            raise ConflictError(f"A device already uses {management_ip}.")


def list_devices(
    role: str | None = None,
    status: str | None = None,
    project_id: int | None = None,
) -> list[Device]:
    """Devices, optionally restricted to one project.

    HTTP routes always pass ``project_id``; ``None`` is for internal callers
    (the monitoring scheduler, CLI scripts) that work across projects.
    """
    query = db.session.query(Device)
    if project_id is not None:
        query = query.filter(Device.project_id == project_id)
    if role:
        query = query.filter(Device.role == role)
    if status:
        query = query.filter(Device.status == status)
    return query.order_by(Device.hostname, Device.id).all()


def get_device(device_id: int, project_id: int | None = None) -> Device:
    device = db.session.get(Device, device_id)
    # A device in another project is indistinguishable from a missing one.
    if device is None or (project_id is not None and device.project_id != project_id):
        raise NotFoundError(f"Device {device_id} was not found.")
    return device


def get_device_by_hostname(hostname: str, project_id: int | None = None) -> Device:
    query = db.session.query(Device).filter(Device.hostname == hostname)
    if project_id is not None:
        query = query.filter(Device.project_id == project_id)
    matches = query.all()
    if not matches:
        raise NotFoundError(f"Device {hostname} was not found.")
    if len(matches) > 1:
        raise ValidationError(
            f"Hostname {hostname} exists in several projects; a project is required."
        )
    return matches[0]


def create_device(payload: dict, project) -> Device:
    data = _validate(DeviceCreateSchema, payload, project)
    _assert_unique(project.id, data.hostname, data.management_ip)

    fields = data.model_dump(exclude={"credential"})
    credential = data.credential
    if credential is not None:
        _require_cipher()

    device = Device(project_id=project.id, **fields)
    device.status = "unknown"
    db.session.add(device)
    db.session.flush()
    if credential is not None:
        _store_credential(device, credential)
    db.session.commit()
    return device


def update_device(device_id: int, payload: dict, project) -> Device:
    device = get_device(device_id, project.id)
    data = _validate(DeviceUpdateSchema, payload, project)
    changes = data.model_dump(exclude_unset=True, exclude={"credential"})

    _assert_unique(
        project.id,
        changes.get("hostname"),
        changes.get("management_ip"),
        exclude_id=device_id,
    )

    if data.credential is not None:
        _require_cipher()

    for field, value in changes.items():
        setattr(device, field, value)
    if data.credential is not None:
        _store_credential(device, data.credential)
    db.session.commit()
    return device


def _require_cipher() -> None:
    from ..credentials.service import get_cipher

    try:
        get_cipher()
    except ValueError as exc:
        raise ValidationError(
            "Device credentials cannot be stored: the server has no valid "
            "CREDENTIAL_ENCRYPTION_KEY."
        ) from exc


def _store_credential(device: Device, credential) -> None:
    from ..credentials.service import store_device_credential

    store_device_credential(
        device.id, credential.username, credential.password, credential.enable_secret
    )


_ACTIVE_BATCH_STATUSES = ("pending_approval", "approved", "running")


def _assert_no_active_batch(device_id: int) -> None:
    from ..changes.model import ChangeBatch, ChangeRequest

    active = (
        db.session.query(ChangeBatch)
        .join(ChangeRequest, ChangeRequest.batch_id == ChangeBatch.id)
        .filter(
            ChangeRequest.device_id == device_id,
            ChangeBatch.status.in_(_ACTIVE_BATCH_STATUSES),
        )
        .first()
    )
    if active is not None:
        raise ConflictError(
            f"Device {device_id} is a target in an active change batch and "
            "cannot be deleted."
        )


def purge_devices(device_ids: list[int], commit: bool = True) -> None:
    """Delete devices and everything that hangs off them.

    The schema declares ON DELETE CASCADE / SET NULL for these relations but
    SQLite does not enforce foreign keys by default, so they are removed here
    explicitly instead of relying on the database.
    """
    if not device_ids:
        return

    from ..audit.model import AuditLog
    from ..backups.model import ConfigBackup
    from ..changes.model import ChangeRequest
    from ..commands.model import CommandExecution
    from ..credentials.model import DeviceCredential
    from ..monitoring.model import DeviceSnapshot
    from ..topology.model import TopologyLink

    def where(model):
        return model.device_id.in_(device_ids)

    # Changes reference backups, so they go first.
    db.session.query(ChangeRequest).filter(where(ChangeRequest)).delete(
        synchronize_session=False
    )
    db.session.query(ConfigBackup).filter(where(ConfigBackup)).delete(
        synchronize_session=False
    )
    db.session.query(DeviceSnapshot).filter(where(DeviceSnapshot)).delete(
        synchronize_session=False
    )
    db.session.query(DeviceCredential).filter(where(DeviceCredential)).delete(
        synchronize_session=False
    )
    db.session.query(CommandExecution).filter(where(CommandExecution)).update(
        {CommandExecution.device_id: None}, synchronize_session=False
    )
    db.session.query(AuditLog).filter(where(AuditLog)).update(
        {AuditLog.device_id: None}, synchronize_session=False
    )
    db.session.query(TopologyLink).filter(
        db.or_(
            TopologyLink.device_a_id.in_(device_ids),
            TopologyLink.device_b_id.in_(device_ids),
        )
    ).delete(synchronize_session=False)
    db.session.query(Device).filter(Device.id.in_(device_ids)).delete(
        synchronize_session=False
    )
    db.session.expire_all()
    if commit:
        db.session.commit()


def delete_device(device_id: int, project_id: int | None = None) -> None:
    device = get_device(device_id, project_id)
    _assert_no_active_batch(device_id)
    purge_devices([device.id])


def set_device_status(device: Device, status: str) -> Device:
    from datetime import datetime, timezone

    device.status = status
    if status == "online":
        device.last_seen_at = datetime.now(timezone.utc)
    db.session.commit()
    return device


def check_reachability(device: Device) -> tuple[bool, str]:
    """SSH into the device and record the resulting online/offline status.

    Returns (reachable, human readable detail). The detail never contains
    credentials because SSH errors only ever reference user@host:port.
    """
    from ..errors import AppError
    from ..ssh.client import build_client_for_device

    try:
        client = build_client_for_device(device)
        reachable = bool(client.test_connection())
    except AppError as exc:
        set_device_status(device, "offline")
        return False, exc.message
    except Exception:  # pragma: no cover - unexpected transport failure
        set_device_status(device, "offline")
        return False, "SSH connection failed."

    set_device_status(device, "online" if reachable else "offline")
    detail = "SSH connection succeeded." if reachable else "SSH connection failed."
    return reachable, detail
