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
# Where the device lives: a PNETLab emulated node or real hardware. Both are
# reached the same way (SSH to the management IP); the value is kept so the
# inventory, the diagram and reports can tell them apart.
DEVICE_ENVIRONMENTS = ("pnetlab", "physical")


class Device(db.Model):
    __tablename__ = "devices"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Hostname and management IP are unique per project, not globally: two
    # projects may both have an "R1", or reuse an address range.
    hostname = db.Column(db.String(64), nullable=False, index=True)
    management_ip = db.Column(db.String(45), nullable=False, index=True)
    device_type = db.Column(db.String(32), nullable=False)
    role = db.Column(db.String(32), nullable=False)
    ssh_port = db.Column(db.Integer, nullable=False, default=22)
    status = db.Column(db.String(16), nullable=False, default="unknown")
    monitoring_enabled = db.Column(db.Boolean, nullable=False, default=True)
    environment = db.Column(
        db.String(16), nullable=False, default="pnetlab", server_default="pnetlab"
    )
    description = db.Column(db.String(255))
    # Position on the topology canvas.
    pos_x = db.Column(db.Float)
    pos_y = db.Column(db.Float)
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

    __table_args__ = (
        db.UniqueConstraint("project_id", "hostname", name="uq_devices_project_hostname"),
        db.UniqueConstraint(
            "project_id", "management_ip", name="uq_devices_project_management_ip"
        ),
    )

    def to_dict(self) -> dict:
        """Public representation. Credentials are never included."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "environment": self.environment,
            "has_credential": getattr(self, "credential", None) is not None,
            "pos_x": self.pos_x,
            "pos_y": self.pos_y,
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
