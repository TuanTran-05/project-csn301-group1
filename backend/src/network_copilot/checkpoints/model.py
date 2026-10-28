from datetime import datetime, timezone

from ..extensions import db, in_check

CHECKPOINT_STATES = ("active", "undone", "discarded")
CHECKPOINT_ACTIONS = ("undo", "redo")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConfigCheckpoint(db.Model):
    """One applied configuration change on one device, as an undo/redo step.

    ``pre_backup`` is the running-config captured just before the change (the
    state Undo returns to). ``post_backup`` is the running-config after it (the
    state Redo returns to); it is captured when the step is undone, because
    that is the moment the backend reads the device anyway.

    Per device the steps form a stack: only the newest ``active`` step can be
    undone, only the most recently ``undone`` step can be redone, and a new
    change on the device ``discards`` every undone step after it.
    """

    __tablename__ = "config_checkpoints"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    change_request_id = db.Column(
        db.Integer, db.ForeignKey("change_requests.id", ondelete="SET NULL"), index=True
    )
    batch_id = db.Column(db.Integer, db.ForeignKey("change_batches.id", ondelete="SET NULL"))
    name = db.Column(db.String(120), nullable=False)
    commands = db.Column(db.JSON, nullable=False, default=list)
    pre_backup_id = db.Column(db.Integer, db.ForeignKey("config_backups.id", ondelete="SET NULL"))
    post_backup_id = db.Column(db.Integer, db.ForeignKey("config_backups.id", ondelete="SET NULL"))
    state = db.Column(db.String(16), nullable=False, default="active", server_default="active")
    pending_change_id = db.Column(
        db.Integer, db.ForeignKey("change_requests.id", ondelete="SET NULL")
    )
    pending_action = db.Column(db.String(8))
    last_result = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)
    undone_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        in_check("state", CHECKPOINT_STATES, "state_valid"),
        db.CheckConstraint(
            "pending_action IS NULL OR pending_action IN ('undo', 'redo')",
            name="pending_action_valid",
        ),
        db.Index("ix_config_checkpoints_device_state", "device_id", "state"),
    )

    device = db.relationship("Device")
    pending_change = db.relationship("ChangeRequest", foreign_keys=[pending_change_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def to_dict(self, *, can_undo: bool = False, can_redo: bool = False) -> dict:
        pending = self.pending_change
        pending_live = pending is not None and pending.status in {
            "pending_approval", "approved", "running"
        }
        return {
            "id": self.id,
            "project_id": self.project_id,
            "device_id": self.device_id,
            "hostname": self.device.hostname if self.device else None,
            "change_request_id": self.change_request_id,
            "batch_id": self.batch_id,
            "name": self.name,
            "commands": self.commands or [],
            "state": self.state,
            "has_post_state": self.post_backup_id is not None,
            "pending_action": self.pending_action if pending_live else None,
            "pending_change_id": self.pending_change_id if pending_live else None,
            "pending_change_status": pending.status if pending_live else None,
            "last_result": self.last_result,
            "created_by": self.created_by.username if self.created_by else None,
            "created_at": _iso(self.created_at),
            "undone_at": _iso(self.undone_at),
            "can_undo": can_undo,
            "can_redo": can_redo,
        }


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
