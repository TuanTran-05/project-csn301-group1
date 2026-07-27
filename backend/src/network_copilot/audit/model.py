from datetime import datetime, timezone

from ..extensions import db

RESULTS = ("success", "failure", "blocked")


class AuditLog(db.Model):
    """Immutable record of who did what, to which device, and how it ended."""

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(64), nullable=False, index=True)
    result = db.Column(db.String(16), nullable=False, index=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    username = db.Column(db.String(64))
    device_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    message = db.Column(db.String(512))
    details = db.Column(db.JSON)
    source_ip = db.Column(db.String(45))
    request_id = db.Column(db.String(36))
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "result": self.result,
            "user_id": self.user_id,
            "username": self.username,
            "device_id": self.device_id,
            "message": self.message,
            "details": self.details,
            "source_ip": self.source_ip,
            "request_id": self.request_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<AuditLog {self.action} {self.result}>"
