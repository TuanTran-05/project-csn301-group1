from datetime import datetime, timezone

from ..extensions import db


class ConfigBackup(db.Model):
    """A `show running-config` capture taken before a change is applied."""

    __tablename__ = "config_backups"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(
        db.Integer,
        db.ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    change_request_id = db.Column(db.Integer, index=True)
    running_config = db.Column(db.Text, nullable=False)
    reason = db.Column(db.String(128), nullable=False, default="pre_change")
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    device = db.relationship("Device")

    def to_dict(self, include_config: bool = False) -> dict:
        payload = {
            "id": self.id,
            "device_id": self.device_id,
            "change_request_id": self.change_request_id,
            "reason": self.reason,
            "size_bytes": len(self.running_config or ""),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_config:
            payload["running_config"] = self.running_config
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ConfigBackup device_id={self.device_id} {self.reason}>"
