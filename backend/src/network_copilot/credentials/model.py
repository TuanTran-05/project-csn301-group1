from datetime import datetime, timezone

from ..extensions import db


class DeviceCredential(db.Model):
    """SSH credential for a device. The password is only ever stored encrypted."""

    __tablename__ = "device_credentials"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(
        db.Integer,
        db.ForeignKey("devices.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    username = db.Column(db.String(64), nullable=False)
    password_encrypted = db.Column(db.Text, nullable=False)
    enable_secret_encrypted = db.Column(db.Text)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    device = db.relationship("Device", backref=db.backref("credential", uselist=False))

    def to_dict(self) -> dict:
        """Metadata only. Secrets are never serialised."""
        return {
            "id": self.id,
            "device_id": self.device_id,
            "username": self.username,
            "has_enable_secret": self.enable_secret_encrypted is not None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<DeviceCredential device_id={self.device_id}>"
