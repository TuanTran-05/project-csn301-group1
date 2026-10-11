"""Port-channels, loopbacks and router-on-a-stick subinterfaces

Revision ID: b2d4e6f8a0c1
Revises: a1c2d3e4f5b6
Create Date: 2026-10-11

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b2d4e6f8a0c1"
down_revision = "a1c2d3e4f5b6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("topology_links") as batch_op:
        batch_op.add_column(sa.Column("channel_id", sa.Integer()))
        batch_op.create_check_constraint(
            "ck_topology_links_channel_range", "channel_id IS NULL OR channel_id BETWEEN 1 AND 48"
        )

    op.create_table(
        "design_loopbacks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.UniqueConstraint("device_id", "number", name="uq_design_loopbacks_number"),
        sa.UniqueConstraint("project_id", "ip", name="uq_design_loopbacks_ip"),
        sa.CheckConstraint("number BETWEEN 0 AND 99", name="ck_design_loopbacks_number_range"),
    )
    op.create_index("ix_design_loopbacks_project_id", "design_loopbacks", ["project_id"])
    op.create_index("ix_design_loopbacks_device_id", "design_loopbacks", ["device_id"])

    op.create_table(
        "design_subifs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interface", sa.String(40), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("prefix_length", sa.Integer(), nullable=False),
        sa.UniqueConstraint("device_id", "interface", "vlan_id", name="uq_design_subifs_vlan"),
        sa.CheckConstraint("prefix_length BETWEEN 8 AND 30", name="ck_design_subifs_prefix_range"),
        sa.ForeignKeyConstraint(
            ["project_id", "vlan_id"],
            ["design_vlans.project_id", "design_vlans.vlan_id"],
            ondelete="NO ACTION",
            name="fk_design_subifs_vlan",
        ),
    )
    op.create_index("ix_design_subifs_project_id", "design_subifs", ["project_id"])
    op.create_index("ix_design_subifs_device_id", "design_subifs", ["device_id"])


def downgrade():
    op.drop_index("ix_design_subifs_device_id", table_name="design_subifs")
    op.drop_index("ix_design_subifs_project_id", table_name="design_subifs")
    op.drop_table("design_subifs")
    op.drop_index("ix_design_loopbacks_device_id", table_name="design_loopbacks")
    op.drop_index("ix_design_loopbacks_project_id", table_name="design_loopbacks")
    op.drop_table("design_loopbacks")
    with op.batch_alter_table("topology_links") as batch_op:
        batch_op.drop_constraint("ck_topology_links_channel_range", type_="check")
        batch_op.drop_column("channel_id")
