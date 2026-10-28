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


class ProjectSshDefault(db.Model):
    """The SSH login most devices of a project share, stored encrypted.

    New devices take it automatically, and the device form fills it in, so an
    operator types the lab login once per project instead of once per device.
    The password is never serialised.
    """

    __tablename__ = "project_ssh_defaults"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    username = db.Column(db.String(64), nullable=False)
    password_encrypted = db.Column(db.Text, nullable=False)
    enable_secret_encrypted = db.Column(db.Text)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        """Metadata only. Secrets are never serialised."""
        return {
            "username": self.username,
            "has_password": bool(self.password_encrypted),
            "has_enable_secret": self.enable_secret_encrypted is not None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ProjectPnetlabLogin(db.Model):
    """The PNETLab address and account a project imports from, password encrypted.

    Saved after a successful "Tải danh sách node" when the user ticks
    "Ghi nhớ", so the import and console-bootstrap forms do not need it
    typed again. The password is never serialised.
    """

    __tablename__ = "project_pnetlab_logins"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    url = db.Column(db.String(255), nullable=False)
    username = db.Column(db.String(128), nullable=False)
    password_encrypted = db.Column(db.Text, nullable=False)
    lab = db.Column(db.String(255), nullable=False, default="", server_default="")
    api_mode = db.Column(db.String(8), nullable=False, default="auto", server_default="auto")
    verify_tls = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        """Everything the form needs except the password."""
        return {
            "url": self.url,
            "username": self.username,
            "lab": self.lab or "",
            "api_mode": self.api_mode,
            "verify_tls": bool(self.verify_tls),
            "has_password": bool(self.password_encrypted),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
