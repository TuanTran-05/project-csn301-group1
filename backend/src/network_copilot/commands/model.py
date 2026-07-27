from datetime import datetime, timezone

from ..extensions import db

EXECUTION_STATUSES = ("success", "failed", "blocked")


class CommandExecution(db.Model):
    """Audit trail of every command the backend was asked to run."""

    __tablename__ = "command_executions"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    command = db.Column(db.String(512), nullable=False)
    output = db.Column(db.Text)
    status = db.Column(db.String(16), nullable=False)
    reason = db.Column(db.String(512))
    source = db.Column(db.String(16), nullable=False, default="api")
    duration_ms = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    device = db.relationship("Device")
    user = db.relationship("User")

    def to_dict(self, include_output: bool = True) -> dict:
        payload = {
            "id": self.id,
            "device_id": self.device_id,
            "user_id": self.user_id,
            "command": self.command,
            "status": self.status,
            "reason": self.reason,
            "source": self.source,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_output:
            payload["output"] = self.output
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<CommandExecution {self.command!r} {self.status}>"
