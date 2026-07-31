from datetime import datetime, timezone

from ..extensions import db

CHANGE_STATUSES = (
    "pending_approval",
    "approved",
    "running",
    "success",
    "failed",
    "cancelled",
)

RISK_LEVELS = ("low", "medium", "high")


class ChangeRequest(db.Model):
    """A configuration change moving through Preview -> Approve -> Apply."""

    __tablename__ = "change_requests"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    requested_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    approved_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL")
    )

    description = db.Column(db.String(255))
    commands = db.Column(db.JSON, nullable=False, default=list)
    verification_commands = db.Column(db.JSON, nullable=False, default=list)
    rollback_commands = db.Column(db.JSON, nullable=False, default=list)
    warnings = db.Column(db.JSON, nullable=False, default=list)

    risk_level = db.Column(db.String(16), nullable=False, default="low")
    status = db.Column(db.String(32), nullable=False, default="pending_approval")
    source = db.Column(db.String(16), nullable=False, default="api")

    # Set when any command matches an inherently dangerous pattern (write
    # erase, reload, shutdown, no router ospf, ...) or touches a reserved
    # VLAN. The change still gets a Preview - it is not blocked - but Apply
    # additionally requires the caller to type the device hostname back,
    # a stronger confirmation than the single Approve/Apply click every
    # other change already goes through.
    requires_confirmation = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )

    apply_output = db.Column(db.Text)
    verification_output = db.Column(db.JSON)
    error_message = db.Column(db.String(512))

    backup_id = db.Column(db.Integer, db.ForeignKey("config_backups.id"))

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    approved_at = db.Column(db.DateTime)
    applied_at = db.Column(db.DateTime)

    device = db.relationship("Device")
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "description": self.description,
            "device": (
                {
                    "id": self.device.id,
                    "hostname": self.device.hostname,
                    "management_ip": self.device.management_ip,
                    "role": self.device.role,
                    "device_type": self.device.device_type,
                }
                if self.device
                else {"id": self.device_id}
            ),
            "commands": self.commands or [],
            "verification_commands": self.verification_commands or [],
            "rollback_commands": self.rollback_commands or [],
            "warnings": self.warnings or [],
            "requested_by_id": self.requested_by_id,
            "approved_by_id": self.approved_by_id,
            "backup_id": self.backup_id,
            "apply_output": self.apply_output,
            "verification_output": self.verification_output,
            "error_message": self.error_message,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ChangeRequest {self.id} {self.status}>"
