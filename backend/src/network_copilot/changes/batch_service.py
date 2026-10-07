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

from sqlalchemy import update as sa_update

from ..audit.service import record_event
from ..devices.model import Device
from ..errors import InvalidStateError, NotFoundError, ValidationError
from ..extensions import db
from .model import ChangeBatch
from .service import _apply_approved_change, _fail, _now, prepare_change


@dataclass(frozen=True)
class BatchOperation:
    device_hostnames: list[str]
    execution_mode: str
    commands: list[str]
    verification_commands: list[str]


def _resolve_targets(
    hostnames: list[str], project_id: int | None = None
) -> list[Device]:
    """Turn hostnames (or "*") into devices.

    With ``project_id`` the lookup, including the "*" wildcard, only sees that
    project's devices. Without it (internal callers) a hostname that exists in
    several projects is ambiguous and the batch builder below rejects any
    result that spans projects.
    """
    if not hostnames:
        raise ValidationError("At least one device hostname (or '*') is required.")

    query = db.session.query(Device)
    if project_id is not None:
        query = query.filter(Device.project_id == project_id)
    if hostnames == ["*"]:
        return query.order_by(Device.hostname, Device.id).all()
    if "*" in hostnames:
        raise ValidationError("'*' cannot be mixed with explicit hostnames.")
    unique = sorted(set(hostnames))
    devices = query.filter(Device.hostname.in_(unique)).all()
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
    project_id: int | None = None,
) -> ChangeBatch:
    """Resolve every operation's targets and build one ChangeRequest per
    device. Either the whole batch is created, or nothing is - a conflicting
    or unknown target raises before any row is added to the session.

    A batch belongs to exactly one project. Routes pass ``project_id`` so every
    hostname, and the "*" wildcard, only resolves inside it."""
    if not operations:
        raise ValidationError("At least one batch operation is required.")

    resolved: dict[int, tuple[Device, BatchOperation]] = {}
    for operation in operations:
        devices = _resolve_targets(operation.device_hostnames, project_id)
        for device in devices:
            previous = resolved.get(device.id)
            if previous and previous[1] != operation:
                raise ValidationError(
                    f"Device '{device.hostname}' has conflicting batch operations."
                )
            resolved[device.id] = (device, operation)

    if not resolved:
        # Reachable when every operation is a wildcard ("*") and the device
        # table is empty - _resolve_targets lets an empty wildcard result
        # through since it isn't a malformed request, it just has nothing to
        # target. Catch it here instead of letting max() below raise a raw
        # ValueError.
        raise ValidationError("The batch did not resolve to any device.")

    project_ids = {device.project_id for device, _ in resolved.values()}
    if len(project_ids) != 1:
        raise ValidationError(
            "A batch can only target devices of a single project; pass a project."
        )

    batch = ChangeBatch(
        project_id=project_ids.pop(),
        requested_by_id=user_id,
        description=(description or "")[:255] or None,
        status="pending_approval",
        source=source,
    )
    try:
        for device, operation in sorted(
            resolved.values(), key=lambda pair: (pair[0].hostname, pair[0].id)
        ):
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


def get_batch(batch_id: int, project_id: int | None = None) -> ChangeBatch:
    batch = db.session.get(ChangeBatch, batch_id)
    if batch is not None and project_id is not None and batch.project_id != project_id:
        batch = None
    if batch is None:
        raise NotFoundError(f"Change batch {batch_id} was not found.")
    return batch


def list_batches(limit: int = 100, project_id: int | None = None) -> list[ChangeBatch]:
    query = db.session.query(ChangeBatch)
    if project_id is not None:
        query = query.filter(ChangeBatch.project_id == project_id)
    return (
        query.order_by(ChangeBatch.created_at.desc(), ChangeBatch.id.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )


# -- approve / cancel -------------------------------------------------------


def approve_batch(batch_id: int, user_id: int | None) -> ChangeBatch:
    """Approve the batch and every pending child in the same transaction.

    Children never go through their own individual approve() call - the
    batch approval covers all of them at once.
    """
    batch = get_batch(batch_id)
    if batch.status != "pending_approval":
        raise InvalidStateError(
            f"Only a batch in 'pending_approval' can be approved; "
            f"batch {batch_id} is '{batch.status}'."
        )
    now = _now()
    batch.status = "approved"
    batch.approved_by_id = user_id
    batch.approved_at = now
    for change in batch.changes:
        if change.status == "pending_approval":
            change.status = "approved"
            change.approved_by_id = user_id
            change.approved_at = now
    db.session.commit()
    record_event(
        action="batch.approve",
        result="success",
        user_id=user_id,
        details={
            "batch_id": batch.id,
            "child_count": len(batch.changes),
            "risk_level": batch.risk_level,
        },
    )
    return batch


def cancel_batch(batch_id: int, user_id: int | None) -> ChangeBatch:
    """Cancel the batch and every still-eligible child in the same transaction."""
    batch = get_batch(batch_id)
    if batch.status not in {"pending_approval", "approved"}:
        raise InvalidStateError(
            f"A batch in state '{batch.status}' can no longer be cancelled."
        )
    batch.status = "cancelled"
    for change in batch.changes:
        if change.status in {"pending_approval", "approved"}:
            change.status = "cancelled"
    db.session.commit()
    record_event(
        action="batch.cancel",
        result="success",
        user_id=user_id,
        details={"batch_id": batch.id, "child_count": len(batch.changes)},
    )
    return batch


# -- apply -------------------------------------------------------------------


def aggregate_status(outcomes: list[str]) -> str:
    """Roll many child outcomes up into one batch status."""
    if not outcomes:
        return "failed"
    if all(outcome == "success" for outcome in outcomes):
        return "success"
    if all(outcome == "failed" for outcome in outcomes):
        return "failed"
    return "partial_success"


def apply_batch(
    batch_id: int, user_id: int | None, confirmation: str | None = None
) -> ChangeBatch:
    """Apply every child sequentially, aggregating partial success.

    The batch-level confirmation is validated exactly once, before any SSH
    work starts on any child - never per-child. A dangerous one-device batch
    requires the exact hostname; a dangerous batch with two or more children
    requires exactly "CONFIRM ALL" after trimming surrounding whitespace.
    ``ChangeBatch.confirmation_text`` already computes the right expected
    string for both cases, so a single trimmed-string comparison covers both.
    """
    batch = get_batch(batch_id)
    if batch.status != "approved":
        raise InvalidStateError(
            f"Only an approved batch can be applied; batch {batch_id} is "
            f"'{batch.status}'."
        )

    if batch.requires_confirmation:
        provided = (confirmation or "").strip()
        if provided != batch.confirmation_text:
            raise ValidationError(
                "This batch contains a dangerous command. Confirm by sending "
                f"confirmation equal to {batch.confirmation_text!r}.",
                {"confirmation_required": batch.confirmation_text},
            )

    # Claim the batch atomically: only one concurrent apply request may move
    # it from 'approved' to 'running'. A lost race (rowcount 0) means another
    # request already claimed it, so this one must abort before touching SSH.
    claim = db.session.execute(
        sa_update(ChangeBatch)
        .where(ChangeBatch.id == batch.id, ChangeBatch.status == "approved")
        .values(status="running")
    )
    if claim.rowcount == 0:
        db.session.rollback()
        raise InvalidStateError(
            f"Batch {batch_id} could not be claimed for apply; it may already "
            "have been claimed by another request."
        )
    batch.status = "running"
    db.session.commit()

    outcomes: list[str] = []
    changes = sorted(batch.changes, key=lambda item: item.target_hostname)
    for index, change in enumerate(changes):
        if index:
            db.session.expire(change)
            db.session.refresh(change)
        try:
            _apply_approved_change(change, user_id)
        except Exception as exc:  # pragma: no cover - defensive: one child must never abort the batch
            db.session.rollback()
            _fail(change, f"Unexpected error while applying: {exc}", user_id)
        outcomes.append(change.status)

    batch.status = aggregate_status(outcomes)
    batch.applied_at = _now()
    db.session.commit()

    record_event(
        action="batch.apply",
        result=batch.status,
        user_id=user_id,
        details={
            "batch_id": batch.id,
            "child_count": len(batch.changes),
            "hostnames": [change.target_hostname for change in batch.changes],
            "outcomes": outcomes,
        },
    )
    return batch
