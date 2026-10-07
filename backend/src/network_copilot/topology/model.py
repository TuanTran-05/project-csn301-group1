from datetime import datetime, timezone

from ..extensions import db

LINK_TYPES = ("physical", "routed", "trunk")


class TopologyLink(db.Model):
    """A cable between two device interfaces in a project's network design.

    The diagram is a design: it records what the operator intends, and it is
    only turned into device configuration through the normal
    Preview -> Approve -> Apply workflow.
    """

    __tablename__ = "topology_links"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_a_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    interface_a = db.Column(db.String(40), nullable=False)
    device_b_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    interface_b = db.Column(db.String(40), nullable=False)

    link_type = db.Column(db.String(16), nullable=False, default="physical")
    # routed links: the point-to-point subnet and each end's address.
    network = db.Column(db.String(43))
    ip_a = db.Column(db.String(45))
    ip_b = db.Column(db.String(45))
    # trunk links: allowed VLAN list in IOS syntax, e.g. "10,20,30-40".
    allowed_vlans = db.Column(db.String(255))
    # Whether generated configuration brings the interfaces up.
    bring_up = db.Column(
        db.Boolean, nullable=False, default=True, server_default=db.true()
    )
    description = db.Column(db.String(255))
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    device_a = db.relationship("Device", foreign_keys=[device_a_id])
    device_b = db.relationship("Device", foreign_keys=[device_b_id])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "device_a_id": self.device_a_id,
            "hostname_a": self.device_a.hostname if self.device_a else None,
            "interface_a": self.interface_a,
            "device_b_id": self.device_b_id,
            "hostname_b": self.device_b.hostname if self.device_b else None,
            "interface_b": self.interface_b,
            "link_type": self.link_type,
            "network": self.network,
            "ip_a": self.ip_a,
            "ip_b": self.ip_b,
            "allowed_vlans": self.allowed_vlans,
            "bring_up": self.bring_up,
            "description": self.description,
        }
