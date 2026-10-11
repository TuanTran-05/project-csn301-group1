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


def _validate(schema, payload: dict):
    try:
        return schema.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            "Device payload failed validation.", _format_pydantic_errors(exc)
        ) from exc


def _assert_unique(hostname: str | None, management_ip: str | None, exclude_id=None):
    if hostname is not None:
        query = db.session.query(Device).filter(Device.hostname == hostname)
        if exclude_id is not None:
            query = query.filter(Device.id != exclude_id)
        if query.first() is not None:
            raise ConflictError(f"A device named {hostname} already exists.")

    if management_ip is not None:
        query = db.session.query(Device).filter(Device.management_ip == management_ip)
        if exclude_id is not None:
            query = query.filter(Device.id != exclude_id)
        if query.first() is not None:
            raise ConflictError(f"A device already uses {management_ip}.")


def list_devices(role: str | None = None, status: str | None = None) -> list[Device]:
    query = db.session.query(Device)
    if role:
        query = query.filter(Device.role == role)
    if status:
        query = query.filter(Device.status == status)
    from ..governance.visibility import devices
    return devices(query.order_by(Device.hostname).all())


def get_device(device_id: int) -> Device:
    device = db.session.get(Device, device_id)
    if device is None:
        raise NotFoundError(f"Device {device_id} was not found.")
    from ..governance.visibility import request_user
    from ..governance.access import require
    user = request_user()
    if user:
        require(user, "device.read", device=device)
    return device


def get_device_by_hostname(hostname: str) -> Device:
    device = (
        db.session.query(Device).filter(Device.hostname == hostname).one_or_none()
    )
    if device is None:
        raise NotFoundError(f"Device {hostname} was not found.")
    from ..governance.visibility import request_user
    from ..governance.access import require
    user = request_user()
    if user:
        require(user, "device.read", device=device)
    return device


def create_device(payload: dict) -> Device:
    data = _validate(DeviceCreateSchema, payload)
    _assert_unique(data.hostname, data.management_ip)

    device = Device(**data.model_dump())
    device.status = "unknown"
    db.session.add(device)
    db.session.commit()
    return device


def update_device(device_id: int, payload: dict) -> Device:
    device = get_device(device_id)
    data = _validate(DeviceUpdateSchema, payload)
    changes = data.model_dump(exclude_unset=True)
    from ..governance.model import DeviceLease
    if db.session.get(DeviceLease, device_id):
        raise ConflictError("Device is locked by a deployment.")
    if any(value is None for key, value in changes.items() if key not in {"description", "recovery_instructions"}):
        from ..errors import ValidationError
        raise ValidationError("Required device fields cannot be null.")

    _assert_unique(
        changes.get("hostname"), changes.get("management_ip"), exclude_id=device_id
    )

    for field, value in changes.items():
        setattr(device, field, value)
    db.session.commit()
    return device


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


def delete_device(device_id: int) -> None:
    device = get_device(device_id)
    _assert_no_active_batch(device_id)
    from ..changes.model import ChangeRequest
    if db.session.query(ChangeRequest).filter_by(device_id=device_id).first():
        raise ConflictError("Device has change history and must be retained for audit.")
    from ..backups.model import ConfigBackup
    from ..commands.model import CommandExecution
    from ..monitoring.model import DeviceSnapshot
    from ..governance.model import Assignment, Draft, Grant, Reservation
    for model in (ConfigBackup, CommandExecution, DeviceSnapshot, Assignment, Draft, Reservation, Grant):
        if db.session.query(model).filter_by(device_id=device_id).first():
            raise ConflictError("Device has historical or teaching records; retain it for audit.")
    db.session.delete(device)
    db.session.commit()


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
