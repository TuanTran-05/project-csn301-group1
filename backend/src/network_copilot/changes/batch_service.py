"""Atomic multi-device previews: one AI turn -> many device-scoped changes.

A batch groups several ``ChangeRequest`` previews (one per targeted device)
under a single ``ChangeBatch``. Target resolution is a snapshot taken at
preview time - a wildcard ("*") freezes the current device set, it never
picks up devices created after the preview exists. Resolution and conflict
detection happen entirely in memory before anything touches the database, so
a conflicting or unknown target rolls back the whole preview: either every
device in the batch gets a ChangeRequest, or none of them do.
"""

from dataclasses import dataclass

from ..devices.model import Device
from ..errors import NotFoundError, ValidationError
from ..extensions import db
from .model import ChangeBatch
from .service import prepare_change


@dataclass(frozen=True)
class BatchOperation:
    device_hostnames: list[str]
    execution_mode: str
    commands: list[str]
    verification_commands: list[str]


def _resolve_targets(hostnames: list[str]) -> list[Device]:
    if not hostnames:
        raise ValidationError("At least one device hostname (or '*') is required.")
    if hostnames == ["*"]:
        return db.session.query(Device).order_by(Device.hostname).all()
    if "*" in hostnames:
        raise ValidationError("'*' cannot be mixed with explicit hostnames.")
    unique = sorted(set(hostnames))
    devices = db.session.query(Device).filter(Device.hostname.in_(unique)).all()
    found = {device.hostname for device in devices}
    missing = [hostname for hostname in unique if hostname not in found]
    if missing:
        raise ValidationError("Unknown batch targets.", {"device_hostnames": missing})
    return sorted(devices, key=lambda device: device.hostname)


def _risk_rank(level: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}[level]


def create_batch_preview(
    user_id: int | None,
    operations: list[BatchOperation],
    description: str | None,
    source: str = "ai",
) -> ChangeBatch:
    """Resolve every operation's targets and build one ChangeRequest per
    device. Either the whole batch is created, or nothing is - a conflicting
    or unknown target raises before any row is added to the session."""
    if not operations:
        raise ValidationError("At least one batch operation is required.")

    resolved: dict[str, tuple[Device, BatchOperation]] = {}
    for operation in operations:
        devices = _resolve_targets(operation.device_hostnames)
        for device in devices:
            previous = resolved.get(device.hostname)
            if previous and previous[1] != operation:
                raise ValidationError(
                    f"Device '{device.hostname}' has conflicting batch operations."
                )
            resolved[device.hostname] = (device, operation)

    if not resolved:
        # Reachable when every operation is a wildcard ("*") and the device
        # table is empty - _resolve_targets lets an empty wildcard result
        # through since it isn't a malformed request, it just has nothing to
        # target. Catch it here instead of letting max() below raise a raw
        # ValueError.
        raise ValidationError("The batch did not resolve to any device.")

    batch = ChangeBatch(
        requested_by_id=user_id,
        description=(description or "")[:255] or None,
        status="pending_approval",
        source=source,
    )
    try:
        for hostname in sorted(resolved):
            device, operation = resolved[hostname]
            batch.changes.append(
                prepare_change(
                    user_id,
                    device,
                    operation.commands,
                    operation.verification_commands,
                    description,
                    source,
                    operation.execution_mode,
                )
            )
        batch.requires_confirmation = any(c.requires_confirmation for c in batch.changes)
        batch.risk_level = max((c.risk_level for c in batch.changes), key=_risk_rank)
        db.session.add(batch)
        db.session.flush()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return batch


def get_batch(batch_id: int) -> ChangeBatch:
    batch = db.session.get(ChangeBatch, batch_id)
    if batch is None:
        raise NotFoundError(f"Change batch {batch_id} was not found.")
    return batch


def list_batches(limit: int = 100) -> list[ChangeBatch]:
    return (
        db.session.query(ChangeBatch)
        .order_by(ChangeBatch.created_at.desc(), ChangeBatch.id.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
