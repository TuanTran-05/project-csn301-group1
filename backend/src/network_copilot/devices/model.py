from datetime import datetime, timezone

from ..extensions import db

DEVICE_TYPES = ("cisco_ios", "cisco_asa")
DEVICE_ROLES = (
    "isp",
    "firewall",
    "core",
    "distribution",
    "access",
    "dmz",
    "management",
)
DEVICE_STATUSES = ("unknown", "online", "offline")


class Device(db.Model):
    __tablename__ = "devices"

    id = db.Column(db.Integer, primary_key=True)
    hostname = db.Column(db.String(64), unique=True, nullable=False, index=True)
    management_ip = db.Column(db.String(45), unique=True, nullable=False, index=True)
    device_type = db.Column(db.String(32), nullable=False)
    role = db.Column(db.String(32), nullable=False)
    ssh_port = db.Column(db.Integer, nullable=False, default=22)
    status = db.Column(db.String(16), nullable=False, default="unknown")
    monitoring_enabled = db.Column(db.Boolean, nullable=False, default=True)
    description = db.Column(db.String(255))
    last_seen_at = db.Column(db.DateTime)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        """Public representation. Credentials are never included."""
        return {
            "id": self.id,
            "hostname": self.hostname,
            "management_ip": self.management_ip,
            "device_type": self.device_type,
            "role": self.role,
            "ssh_port": self.ssh_port,
            "status": self.status,
            "monitoring_enabled": self.monitoring_enabled,
            "description": self.description,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Device {self.hostname} ({self.management_ip})>"
