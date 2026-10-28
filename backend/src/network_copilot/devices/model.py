from datetime import datetime, timezone

from ..extensions import db, in_check

# "generic" is a device the backend never logs into (a PC, server, cloud, AP...).
DEVICE_TYPES = ("cisco_ios", "cisco_asa", "paloalto_panos", "generic")
DEVICE_ROLES = (
    "isp",
    "firewall",
    "core",
    "distribution",
    "access",
    "dmz",
    "management",
    # unmanaged devices
    "host",
    "server",
    "internet",
    "wireless",
    "segment",
)
# What the device is on the diagram. Managed kinds map onto Cisco devices,
# unmanaged ones are drawn and cabled but never configured or polled.
MANAGED_KINDS = ("router", "l3_switch", "switch", "firewall")
UNMANAGED_KINDS = ("pc", "server", "cloud", "ap", "segment")
DEVICE_KINDS = MANAGED_KINDS + UNMANAGED_KINDS
# "planned" devices exist in the design only: no management IP or SSH yet.
DEVICE_LIFECYCLES = ("planned", "provisioned")
DEVICE_STATUSES = ("unknown", "online", "offline")
# Where the device lives: a PNETLab emulated node or real hardware. Both are
# reached the same way (SSH to the management IP); the value is kept so the
# inventory, the diagram and reports can tell them apart.
DEVICE_ENVIRONMENTS = ("pnetlab", "physical", "virtual", "physical_lab", "production")


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
    # Empty for unmanaged devices and for planned ones (set when provisioned).
    management_ip = db.Column(db.String(45), index=True)
    kind = db.Column(db.String(16), nullable=False, default="router", server_default="router")
    managed = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    lifecycle = db.Column(
        db.String(12), nullable=False, default="provisioned", server_default="provisioned"
    )
    # For unmanaged hosts: their address ("192.168.10.50/24") and default gateway.
    # For a "segment": the shared network the devices on it sit in.
    data_ip = db.Column(db.String(43))
    data_gateway = db.Column(db.String(45))
    device_type = db.Column(db.String(32), nullable=False)
    role = db.Column(db.String(32), nullable=False)
    ssh_port = db.Column(db.Integer, nullable=False, default=22)
    status = db.Column(db.String(16), nullable=False, default="unknown")
    monitoring_enabled = db.Column(db.Boolean, nullable=False, default=True)
    environment = db.Column(
        db.String(32), nullable=False, default="pnetlab", server_default="pnetlab"
    )
    isolated = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    baseline_revision = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    recovery_instructions = db.Column(db.Text)
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
    groups = db.relationship(
        "Group", secondary="group_devices", back_populates="devices"
    )

    __table_args__ = (
        in_check("device_type", DEVICE_TYPES, "device_type_valid"),
        in_check("role", DEVICE_ROLES, "role_valid"),
        in_check("kind", DEVICE_KINDS, "kind_valid"),
        in_check("lifecycle", DEVICE_LIFECYCLES, "lifecycle_valid"),
        # Only generic devices are unmanaged, and a managed device that is not
        # merely planned must be reachable.
        db.CheckConstraint("(managed = 1) = (device_type <> 'generic')", name="managed_matches_type"),
        db.CheckConstraint(
            "managed = 0 OR lifecycle = 'planned' OR management_ip IS NOT NULL",
            name="provisioned_has_ip",
        ),
        in_check("status", DEVICE_STATUSES, "status_valid"),
        in_check("environment", DEVICE_ENVIRONMENTS, "environment_valid"),
        db.CheckConstraint("ssh_port BETWEEN 1 AND 65535", name="ssh_port_range"),
        db.UniqueConstraint("project_id", "hostname", name="uq_devices_project_hostname"),
        db.UniqueConstraint(
            "project_id", "management_ip", name="uq_devices_project_management_ip"
        ),
    )

    @property
    def deployable(self) -> bool:
        """Can the backend open a session to it (managed and provisioned)?"""
        return bool(self.managed) and self.lifecycle == "provisioned"

    def to_dict(self) -> dict:
        """Public representation. Credentials are never included."""
        return {
            "kind": self.kind,
            "managed": bool(self.managed),
            "lifecycle": self.lifecycle,
            "deployable": self.deployable,
            "data_ip": self.data_ip,
            "data_gateway": self.data_gateway,
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
            "environment": self.environment,
            "isolated": self.isolated,
            "baseline_revision": self.baseline_revision,
            "recovery_instructions": self.recovery_instructions,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Device {self.hostname} ({self.management_ip})>"
