from datetime import datetime, timezone

from ..extensions import db, in_check

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
    # Firewall (ASA) interfaces need a name and a trust level; only used when
    # that end of the link is an ASA.
    nameif_a = db.Column(db.String(48))
    security_a = db.Column(db.Integer)
    nameif_b = db.Column(db.String(48))
    security_b = db.Column(db.Integer)
    description = db.Column(db.String(255))
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        in_check("link_type", LINK_TYPES, "link_type_valid"),
        db.CheckConstraint("device_a_id <> device_b_id", name="distinct_devices"),
        db.CheckConstraint(
            "(security_a IS NULL OR security_a BETWEEN 0 AND 100) AND "
            "(security_b IS NULL OR security_b BETWEEN 0 AND 100)",
            name="security_range",
        ),
        # An interface carries one cable. The same port could still appear as
        # the A end of one link and the B end of another; the service layer
        # rejects that (see topology/service.py::_assert_endpoints_free).
        db.UniqueConstraint("device_a_id", "interface_a", name="uq_topology_links_end_a"),
        db.UniqueConstraint("device_b_id", "interface_b", name="uq_topology_links_end_b"),
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
            "nameif_a": self.nameif_a,
            "security_a": self.security_a,
            "nameif_b": self.nameif_b,
            "security_b": self.security_b,
            "description": self.description,
        }


# -- network design beyond cabling ---------------------------------------------
# What the operator intends the devices to carry: VLANs, access ports, SVIs,
# static routes and OSPF. Turned into configuration only through the normal
# Preview -> Approve -> Apply workflow (see topology/configgen.py).


class DesignVlan(db.Model):
    __tablename__ = "design_vlans"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vlan_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(32), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("project_id", "vlan_id", name="uq_design_vlans_project_vlan"),
        # 1 and 1002-1005 are reserved by the platform.
        db.CheckConstraint(
            "vlan_id BETWEEN 2 AND 4094 AND vlan_id NOT BETWEEN 1002 AND 1005",
            name="vlan_range",
        ),
    )

    def to_dict(self) -> dict:
        return {"id": self.id, "vlan_id": self.vlan_id, "name": self.name}


def _vlan_fk(table: str):
    # A port or SVI can only use a VLAN that is defined, and a VLAN in use
    # cannot be deleted while it has users. NO ACTION (checked at the end of
    # the statement) rather than RESTRICT, so deleting a whole project, which
    # removes the VLANs and their users together, is still allowed.
    return db.ForeignKeyConstraint(
        ["project_id", "vlan_id"],
        ["design_vlans.project_id", "design_vlans.vlan_id"],
        ondelete="NO ACTION",
        name=f"fk_{table}_vlan",
    )


class DesignAccessPort(db.Model):
    __tablename__ = "design_access_ports"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, nullable=False, index=True)
    device_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    interface = db.Column(db.String(40), nullable=False)
    vlan_id = db.Column(db.Integer, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("device_id", "interface", name="uq_design_access_ports_port"),
        _vlan_fk("design_access_ports"),
    )

    device = db.relationship("Device")

    def to_dict(self) -> dict:
        return {"id": self.id, "device_id": self.device_id,
                "hostname": self.device.hostname if self.device else None,
                "interface": self.interface, "vlan_id": self.vlan_id}


class DesignSvi(db.Model):
    __tablename__ = "design_svis"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, nullable=False, index=True)
    device_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vlan_id = db.Column(db.Integer, nullable=False)
    ip = db.Column(db.String(45), nullable=False)
    prefix_length = db.Column(db.Integer, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("device_id", "vlan_id", name="uq_design_svis_device_vlan"),
        db.CheckConstraint("prefix_length BETWEEN 8 AND 30", name="prefix_range"),
        _vlan_fk("design_svis"),
    )

    device = db.relationship("Device")

    def to_dict(self) -> dict:
        return {"id": self.id, "device_id": self.device_id,
                "hostname": self.device.hostname if self.device else None,
                "vlan_id": self.vlan_id, "ip": self.ip, "prefix_length": self.prefix_length}


class DesignStaticRoute(db.Model):
    __tablename__ = "design_static_routes"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    network = db.Column(db.String(45), nullable=False)
    prefix_length = db.Column(db.Integer, nullable=False)
    next_hop = db.Column(db.String(45), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("device_id", "network", "prefix_length", "next_hop",
                            name="uq_design_static_routes_route"),
        db.CheckConstraint("prefix_length BETWEEN 0 AND 32", name="prefix_range"),
    )

    device = db.relationship("Device")

    def to_dict(self) -> dict:
        return {"id": self.id, "device_id": self.device_id,
                "hostname": self.device.hostname if self.device else None,
                "network": self.network, "prefix_length": self.prefix_length,
                "next_hop": self.next_hop}


class DesignOspf(db.Model):
    """Single-area (0) OSPF on a device; its networks come from the design."""

    __tablename__ = "design_ospf"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id = db.Column(
        db.Integer, db.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    process_id = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    router_id = db.Column(db.String(45))

    __table_args__ = (
        db.CheckConstraint("process_id BETWEEN 1 AND 65535", name="process_range"),
    )

    device = db.relationship("Device")

    def to_dict(self) -> dict:
        return {"id": self.id, "device_id": self.device_id,
                "hostname": self.device.hostname if self.device else None,
                "process_id": self.process_id, "router_id": self.router_id}
