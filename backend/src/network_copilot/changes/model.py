from datetime import datetime, timezone

from sqlalchemy import event

from ..extensions import db

CHANGE_STATUSES = (
    "pending_approval",
    "approved",
    "running",
    "success",
    "failed",
    "cancelled",
)

BATCH_STATUSES = (
    "pending_approval", "approved", "running", "success",
    "partial_success", "failed", "cancelled",
)

RISK_LEVELS = ("low", "medium", "high")


class ChangeBatch(db.Model):
    """A batch of configuration changes to be applied together."""

    __tablename__ = "change_batches"

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(32), nullable=False, default="pending_approval")
    risk_level = db.Column(db.String(16), nullable=False, default="low")
    requires_confirmation = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    description = db.Column(db.String(255))
    source = db.Column(db.String(16), nullable=False, default="ai")
    requested_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    approved_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    approved_at = db.Column(db.DateTime)
    applied_at = db.Column(db.DateTime)

    changes = db.relationship(
        "ChangeRequest",
        back_populates="batch",
        order_by="ChangeRequest.id",
        cascade="all, delete-orphan",
    )

    @property
    def confirmation_text(self) -> str | None:
        if not self.requires_confirmation or not self.changes:
            return None
        if len(self.changes) == 1:
            return self.changes[0].target_hostname
        return "CONFIRM ALL"

    def to_dict(self) -> dict:
        sorted_changes = sorted(self.changes, key=lambda c: c.target_hostname or "")
        return {
            "id": self.id,
            "status": self.status,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "description": self.description,
            "source": self.source,
            "requested_by_id": self.requested_by_id,
            "approved_by_id": self.approved_by_id,
            "confirmation_text": self.confirmation_text,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            "changes": [change.to_dict() for change in sorted_changes],
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ChangeBatch {self.id} {self.status}>"


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
    batch_id = db.Column(
        db.Integer, db.ForeignKey("change_batches.id", ondelete="CASCADE"), index=True
    )
    execution_mode = db.Column(db.String(16), nullable=False, default="config", server_default="config")

    # Connection identity frozen at Preview time. Apply must compare these
    # against the live Device row and refuse to connect if they diverge - a
    # rename, re-IP, or device-type change between Preview and Apply means
    # the operator approved a different device than the one Apply would now
    # reach, which is exactly the confused-deputy scenario approval exists
    # to prevent.
    target_hostname = db.Column(db.String(64), nullable=False)
    target_management_ip = db.Column(db.String(45), nullable=False)
    target_ssh_port = db.Column(db.Integer, nullable=False, default=22)
    target_device_type = db.Column(db.String(32), nullable=False)

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
    batch = db.relationship("ChangeBatch", back_populates="changes")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "description": self.description,
            "device": {
                "id": self.device.id if self.device else self.device_id,
                "hostname": self.target_hostname or (self.device.hostname if self.device else None),
                "management_ip": self.target_management_ip
                or (self.device.management_ip if self.device else None),
                "role": self.device.role if self.device else None,
                "device_type": self.target_device_type
                or (self.device.device_type if self.device else None),
            },
            "commands": self.commands or [],
            "verification_commands": self.verification_commands or [],
            "rollback_commands": self.rollback_commands or [],
            "warnings": self.warnings or [],
            "requested_by_id": self.requested_by_id,
            "approved_by_id": self.approved_by_id,
            "batch_id": self.batch_id,
            "execution_mode": self.execution_mode,
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


@event.listens_for(ChangeRequest, "before_insert")
def _freeze_target_identity_if_unset(mapper, connection, target: ChangeRequest) -> None:
    """Safety net for ChangeRequest rows built without prepare_change().

    prepare_change() already freezes target_* explicitly from the Device it
    was handed. This only fills the gap for direct ORM construction (tests,
    scripts) so target_hostname's NOT NULL constraint stays meaningful
    without forcing every caller to know about connection-identity freezing.
    """
    if target.target_hostname is not None:
        return
    from ..devices.model import Device

    device = db.session.get(Device, target.device_id)
    if device is None:
        return
    target.target_hostname = device.hostname
    target.target_management_ip = device.management_ip
    target.target_ssh_port = device.ssh_port
    target.target_device_type = device.device_type
