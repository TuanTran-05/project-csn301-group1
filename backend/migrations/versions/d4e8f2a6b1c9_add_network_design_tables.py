"""Add the network design tables and ASA interface naming on links

VLANs, access ports, SVIs, static routes and OSPF that the topology designer
turns into configuration, plus nameif / security level for ASA link ends.

Revision ID: d4e8f2a6b1c9
Revises: c9b5e7a2d4f8
Create Date: 2026-10-09

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d4e8f2a6b1c9"
down_revision = "c9b5e7a2d4f8"
branch_labels = None
depends_on = None


def _vlan_fk(table):
    return sa.ForeignKeyConstraint(
        ["project_id", "vlan_id"],
        ["design_vlans.project_id", "design_vlans.vlan_id"],
        ondelete="NO ACTION",
        name=f"fk_{table}_vlan",
    )


def upgrade():
    with op.batch_alter_table("topology_links") as batch_op:
        batch_op.add_column(sa.Column("nameif_a", sa.String(48)))
        batch_op.add_column(sa.Column("security_a", sa.Integer()))
        batch_op.add_column(sa.Column("nameif_b", sa.String(48)))
        batch_op.add_column(sa.Column("security_b", sa.Integer()))
        batch_op.create_check_constraint(
            "ck_topology_links_security_range",
            "(security_a IS NULL OR security_a BETWEEN 0 AND 100) AND "
            "(security_b IS NULL OR security_b BETWEEN 0 AND 100)",
        )

    op.create_table(
        "design_vlans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.UniqueConstraint("project_id", "vlan_id", name="uq_design_vlans_project_vlan"),
        sa.CheckConstraint(
            "vlan_id BETWEEN 2 AND 4094 AND vlan_id NOT BETWEEN 1002 AND 1005",
            name="ck_design_vlans_vlan_range",
        ),
    )
    op.create_index("ix_design_vlans_project_id", "design_vlans", ["project_id"])

    op.create_table(
        "design_access_ports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interface", sa.String(40), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("device_id", "interface", name="uq_design_access_ports_port"),
        _vlan_fk("design_access_ports"),
    )
    op.create_index("ix_design_access_ports_project_id", "design_access_ports", ["project_id"])
    op.create_index("ix_design_access_ports_device_id", "design_access_ports", ["device_id"])

    op.create_table(
        "design_svis",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("prefix_length", sa.Integer(), nullable=False),
        sa.UniqueConstraint("device_id", "vlan_id", name="uq_design_svis_device_vlan"),
        sa.CheckConstraint("prefix_length BETWEEN 8 AND 30", name="ck_design_svis_prefix_range"),
        _vlan_fk("design_svis"),
    )
    op.create_index("ix_design_svis_project_id", "design_svis", ["project_id"])
    op.create_index("ix_design_svis_device_id", "design_svis", ["device_id"])

    op.create_table(
        "design_static_routes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("network", sa.String(45), nullable=False),
        sa.Column("prefix_length", sa.Integer(), nullable=False),
        sa.Column("next_hop", sa.String(45), nullable=False),
        sa.UniqueConstraint("device_id", "network", "prefix_length", "next_hop",
                            name="uq_design_static_routes_route"),
        sa.CheckConstraint("prefix_length BETWEEN 0 AND 32", name="ck_design_static_routes_prefix_range"),
    )
    op.create_index("ix_design_static_routes_project_id", "design_static_routes", ["project_id"])
    op.create_index("ix_design_static_routes_device_id", "design_static_routes", ["device_id"])

    op.create_table(
        "design_ospf",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("process_id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("router_id", sa.String(45)),
        sa.UniqueConstraint("device_id", name="uq_design_ospf_device_id"),
        sa.CheckConstraint("process_id BETWEEN 1 AND 65535", name="ck_design_ospf_process_range"),
    )
    op.create_index("ix_design_ospf_project_id", "design_ospf", ["project_id"])


def downgrade():
    op.drop_index("ix_design_ospf_project_id", table_name="design_ospf")
    op.drop_table("design_ospf")
    op.drop_index("ix_design_static_routes_device_id", table_name="design_static_routes")
    op.drop_index("ix_design_static_routes_project_id", table_name="design_static_routes")
    op.drop_table("design_static_routes")
    op.drop_index("ix_design_svis_device_id", table_name="design_svis")
    op.drop_index("ix_design_svis_project_id", table_name="design_svis")
    op.drop_table("design_svis")
    op.drop_index("ix_design_access_ports_device_id", table_name="design_access_ports")
    op.drop_index("ix_design_access_ports_project_id", table_name="design_access_ports")
    op.drop_table("design_access_ports")
    op.drop_index("ix_design_vlans_project_id", table_name="design_vlans")
    op.drop_table("design_vlans")
    with op.batch_alter_table("topology_links") as batch_op:
        batch_op.drop_constraint("ck_topology_links_security_range", type_="check")
        batch_op.drop_column("security_b")
        batch_op.drop_column("nameif_b")
        batch_op.drop_column("security_a")
        batch_op.drop_column("nameif_a")
