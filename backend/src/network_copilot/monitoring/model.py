from datetime import datetime, timezone

from ..extensions import db


class DeviceSnapshot(db.Model):
    """One monitoring poll of one device.

    Raw output is always stored, even when a parser returns nothing, so an
    operator can still see exactly what the device replied.
    """

    __tablename__ = "device_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(
        db.Integer,
        db.ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = db.Column(db.String(16), nullable=False)
    raw_output = db.Column(db.JSON, nullable=False, default=dict)
    parsed_data = db.Column(db.JSON, nullable=False, default=dict)
    error = db.Column(db.String(512))
    duration_ms = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    device = db.relationship("Device")

    def to_dict(self, include_raw: bool = True) -> dict:
        payload = {
            "id": self.id,
            "device_id": self.device_id,
            "status": self.status,
            "parsed_data": self.parsed_data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_raw:
            payload["raw_output"] = self.raw_output
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<DeviceSnapshot device_id={self.device_id} {self.status}>"
