"""Undo / redo of applied configuration, one device step at a time.

Every change that applies successfully in configuration mode becomes a
checkpoint holding the running-config captured just before it. Undo and Redo
never write to a device directly: they read the current running-config,
compute the lines that bring it back to the checkpoint's state
(``diff.restore_commands``) and store them as an ordinary change preview
(``source="restore"``). That preview shows up in the Pending column and goes
through the same Approve -> Apply -> Verify workflow, typed confirmation and
pre-change backup as any other change. Only when it applies successfully does
the checkpoint move to ``undone`` (or back to ``active``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import current_app

from ..audit.service import record_event
from ..backups.model import ConfigBackup
from ..backups.service import capture_backup
from ..changes.model import ChangeBatch, ChangeRequest
from ..devices.model import Device
from ..errors import ConflictError, InvalidStateError, NotFoundError, ValidationError
from ..extensions import db
from .diff import configs_match, restore_commands
from .model import ConfigCheckpoint

logger = logging.getLogger(__name__)

SUPPORTED_DEVICE_TYPES = {"cisco_ios"}
LIVE_STATUSES = {"pending_approval", "approved", "running"}
NAME_MAX = 120
VIETNAM_TIME = timezone(timedelta(hours=7))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return bool(current_app.config.get("CONFIG_CHECKPOINTS_ENABLED", True))


# -- recording ---------------------------------------------------------------


def _default_name(change: ChangeRequest, device: Device) -> str:
    text = (change.description or "").strip()
    if not text and change.batch_id:
        batch = db.session.get(ChangeBatch, change.batch_id)
        text = (batch.description or "").strip() if batch else ""
    if not text:
        body = [c for c in (change.commands or []) if c not in {"configure terminal", "end"}]
        text = " · ".join(body[:3]) + (" …" if len(body) > 3 else "")
    return f"{device.hostname}: {text or 'Thay đổi #' + str(change.id)}"[:NAME_MAX]


def on_change_applied(
    change: ChangeRequest, device: Device, client, post_backup: ConfigBackup | None = None
) -> None:
    """Called once a change has applied and verified. Never raises.

    A restore change produced by Undo/Redo moves its checkpoint; any other
    configuration change records a new checkpoint and discards the redo steps
    the device had (a new edit after an undo ends that redo branch).
    """
    if not enabled():
        return
    try:
        step = (
            db.session.query(ConfigCheckpoint)
            .filter(ConfigCheckpoint.pending_change_id == change.id)
            .one_or_none()
        )
        if step is not None:
            _finish_restore(step, change, device, post_backup)
        else:
            _record(change, device, post_backup)
        db.session.commit()
    except Exception:  # pragma: no cover - must never break an apply
        db.session.rollback()
        logger.exception("Could not record configuration checkpoint for change %s.", change.id)


def _record(change: ChangeRequest, device: Device, post_backup: ConfigBackup | None) -> None:
    if change.execution_mode != "config" or not change.backup_id:
        return  # "write memory" and other EXEC commands leave the running-config as it was
    if device.device_type not in SUPPORTED_DEVICE_TYPES:
        return
    db.session.query(ConfigCheckpoint).filter(
        ConfigCheckpoint.device_id == device.id, ConfigCheckpoint.state == "undone"
    ).update({"state": "discarded", "pending_change_id": None, "pending_action": None},
             synchronize_session=False)
    db.session.add(ConfigCheckpoint(
        project_id=device.project_id,
        device_id=device.id,
        change_request_id=change.id,
        batch_id=change.batch_id,
        name=_default_name(change, device),
        commands=[c for c in (change.commands or []) if c not in {"configure terminal", "end"}],
        pre_backup_id=change.backup_id,
        post_backup_id=post_backup.id if post_backup else None,
        created_by_id=change.requested_by_id,
        state="active",
    ))


def _finish_restore(
    step: ConfigCheckpoint, change: ChangeRequest, device: Device, after: ConfigBackup | None
) -> None:
    action = step.pending_action
    target = _target_backup(step, action)
    result = "Đã hoàn tác." if action == "undo" else "Đã làm lại."
    # The restore is only "exact" if nothing a restore can touch still differs
    # from the saved state.
    if after is None:
        result += " Không đọc lại được cấu hình để đối chiếu."
    elif target is not None and not configs_match(after.running_config, target.running_config):
        result += " Một số dòng vẫn khác cấu hình đã lưu; xem lại thiết bị."
    if action == "undo":
        step.state = "undone"
        step.undone_at = _now()
    else:
        step.state = "active"
        step.undone_at = None
    step.pending_change_id = None
    step.pending_action = None
    step.last_result = result[:255]
    record_event(
        action=f"checkpoint.{action}",
        result="success",
        user_id=change.requested_by_id,
        device_id=device.id,
        project_id=device.project_id,
        details={"checkpoint_id": step.id, "change_id": change.id},
    )


# -- reading -----------------------------------------------------------------


def _live(step: ConfigCheckpoint) -> bool:
    return step.pending_change is not None and step.pending_change.status in LIVE_STATUSES


def _top_active(device_id: int) -> ConfigCheckpoint | None:
    return (
        db.session.query(ConfigCheckpoint)
        .filter(ConfigCheckpoint.device_id == device_id, ConfigCheckpoint.state == "active")
        .order_by(ConfigCheckpoint.created_at.desc(), ConfigCheckpoint.id.desc())
        .first()
    )


def _next_redo(device_id: int) -> ConfigCheckpoint | None:
    # Steps are undone newest first, so the most recently undone one is the
    # oldest undone step: redoing it first replays history in order.
    return (
        db.session.query(ConfigCheckpoint)
        .filter(ConfigCheckpoint.device_id == device_id, ConfigCheckpoint.state == "undone")
        .order_by(ConfigCheckpoint.created_at.asc(), ConfigCheckpoint.id.asc())
        .first()
    )


def _device_busy(device_id: int) -> bool:
    return (
        db.session.query(ChangeRequest.id)
        .filter(ChangeRequest.device_id == device_id, ChangeRequest.status.in_(LIVE_STATUSES))
        .first()
        is not None
    )


def list_checkpoints(project_id: int, limit: int = 50) -> dict:
    steps = (
        db.session.query(ConfigCheckpoint)
        .filter(ConfigCheckpoint.project_id == project_id, ConfigCheckpoint.state != "discarded")
        .order_by(ConfigCheckpoint.created_at.desc(), ConfigCheckpoint.id.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    tops: dict[int, int | None] = {}
    redos: dict[int, int | None] = {}
    busy: dict[int, bool] = {}
    items = []
    for step in steps:
        if step.device_id not in tops:
            top, redo = _top_active(step.device_id), _next_redo(step.device_id)
            tops[step.device_id] = top.id if top else None
            redos[step.device_id] = redo.id if redo else None
            busy[step.device_id] = _device_busy(step.device_id)
        supported = step.device is not None and step.device.device_type in SUPPORTED_DEVICE_TYPES
        free = supported and not busy[step.device_id]
        items.append(step.to_dict(
            can_undo=free and tops[step.device_id] == step.id and step.pre_backup_id is not None,
            can_redo=free and redos[step.device_id] == step.id and step.post_backup_id is not None,
        ))

    undo_next = next((item for item in items if item["can_undo"]), None)
    redo_candidates = [item for item in items if item["can_redo"]]
    redo_next = max(redo_candidates, key=lambda item: item["undone_at"] or "", default=None)
    return {
        "items": items,
        "undo_next_id": undo_next["id"] if undo_next else None,
        "redo_next_id": redo_next["id"] if redo_next else None,
    }


def get_checkpoint(checkpoint_id: int, project_id: int) -> ConfigCheckpoint:
    step = db.session.get(ConfigCheckpoint, checkpoint_id)
    if step is None or step.project_id != project_id:
        raise NotFoundError(f"Checkpoint {checkpoint_id} was not found.")
    return step


def rename(step: ConfigCheckpoint, name: str, user_id: int | None) -> ConfigCheckpoint:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Tên không được để trống.")
    if len(name) > NAME_MAX:
        raise ValidationError(f"Tên tối đa {NAME_MAX} ký tự.")
    step.name = name
    db.session.commit()
    record_event(action="checkpoint.rename", result="success", user_id=user_id,
                 device_id=step.device_id, project_id=step.project_id,
                 details={"checkpoint_id": step.id})
    return step


# -- undo / redo -------------------------------------------------------------


def _target_backup(step: ConfigCheckpoint, action: str) -> ConfigBackup | None:
    backup_id = step.pre_backup_id if action == "undo" else step.post_backup_id
    return db.session.get(ConfigBackup, backup_id) if backup_id else None


def request_restore(step: ConfigCheckpoint, action: str, user_id: int | None) -> tuple[ChangeRequest, list[str]]:
    """Read the device and stage the change that undoes/redoes ``step``.

    Returns the new pending change and the config lines left untouched.
    """
    from ..changes import service as change_service

    if action not in {"undo", "redo"}:
        raise ValidationError("action must be 'undo' or 'redo'.")
    device = db.session.get(Device, step.device_id)
    if device is None:
        raise NotFoundError("The device of this step no longer exists.")
    if device.device_type not in SUPPORTED_DEVICE_TYPES:
        raise ValidationError("Hoàn tác/làm lại hiện chỉ hỗ trợ thiết bị Cisco IOS.")
    if _live(step) or _device_busy(device.id):
        raise ConflictError(
            f"{device.hostname} đang có thay đổi chờ duyệt hoặc đang chạy; xử lý nó trước."
        )
    if action == "undo":
        top = _top_active(device.id)
        if step.state != "active" or top is None or top.id != step.id:
            raise InvalidStateError(
                "Chỉ hoàn tác được bước mới nhất còn hiệu lực của thiết bị này."
            )
    else:
        nxt = _next_redo(device.id)
        if step.state != "undone" or nxt is None or nxt.id != step.id:
            raise InvalidStateError("Chỉ làm lại được bước vừa hoàn tác gần nhất của thiết bị này.")
        if step.post_backup_id is None:
            raise InvalidStateError("Bước này không còn cấu hình sau thay đổi để làm lại.")

    target = _target_backup(step, action)
    if target is None:
        raise InvalidStateError("Bản sao lưu cấu hình của bước này không còn.")

    # The current running-config: what the restore starts from, and (for an
    # undo) the state Redo will return to. It is also the newest backup, so
    # the preview's baseline is exactly this config and Apply refuses if the
    # device changes before it runs.
    current = capture_backup(device, reason="checkpoint_snapshot")
    plan = restore_commands(current.running_config, target.running_config)
    if plan.empty:
        raise ConflictError(
            "Cấu hình hiện tại đã giống trạng thái cần khôi phục; không có gì để thay đổi."
        )
    if action == "undo":
        step.post_backup_id = current.id

    verb = "Hoàn tác" if action == "undo" else "Làm lại"
    description = f"{verb}: {step.name}"[:255]
    change = change_service.create_preview(
        user_id,
        device_id=device.id,
        commands=["configure terminal", *plan.commands, "end"],
        description=description,
        source="restore",
        execution_mode="config",
        project_id=step.project_id,
    )
    saved_at = target.created_at or _now()
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=timezone.utc)
    saved_at = saved_at.astimezone(VIETNAM_TIME).strftime("%H:%M %d/%m/%Y")
    notes = [
        f"{verb} đưa {device.hostname} về đúng cấu hình đã lưu lúc {saved_at}; "
        "mọi thay đổi khác trên thiết bị sau thời điểm đó cũng bị gỡ.",
    ]
    if plan.skipped:
        notes.append("Không tự khôi phục (giữ nguyên): " + ", ".join(plan.skipped[:10]))
    change.warnings = list(change.warnings or []) + notes
    step.pending_change_id = change.id
    step.pending_action = action
    step.last_result = None
    db.session.commit()
    record_event(
        action=f"checkpoint.{action}_preview",
        result="success",
        user_id=user_id,
        device_id=device.id,
        project_id=step.project_id,
        details={"checkpoint_id": step.id, "change_id": change.id, "commands": len(plan.commands)},
    )
    return change, plan.skipped
