"""Topology versions (snapshots of the diagram and design)

Revision ID: a1c2d3e4f5b6
Revises: f6a0b4c8d3e2
Create Date: 2026-10-11

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a1c2d3e4f5b6"
down_revision = "f6a0b4c8d3e2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "topology_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("note", sa.String(255)),
        sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("link_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("document", sa.Text(), nullable=False),
    )
    op.create_index("ix_topology_versions_project_id", "topology_versions", ["project_id"])


def downgrade():
    op.drop_index("ix_topology_versions_project_id", table_name="topology_versions")
    op.drop_table("topology_versions")
