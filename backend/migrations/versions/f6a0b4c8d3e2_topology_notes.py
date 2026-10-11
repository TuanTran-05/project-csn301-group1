"""Canvas notes and groups

Revision ID: f6a0b4c8d3e2
Revises: e5f9a3b7c2d1
Create Date: 2026-10-11

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f6a0b4c8d3e2"
down_revision = "e5f9a3b7c2d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "topology_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False, server_default="note"),
        sa.Column("text", sa.String(500), nullable=False),
        sa.Column("x", sa.Float(), nullable=False, server_default="0"),
        sa.Column("y", sa.Float(), nullable=False, server_default="0"),
        sa.Column("width", sa.Float(), nullable=False, server_default="160"),
        sa.Column("height", sa.Float(), nullable=False, server_default="90"),
        sa.Column("color", sa.String(16)),
        sa.CheckConstraint("kind IN ('note', 'group')", name="ck_topology_notes_kind_valid"),
        sa.CheckConstraint("width > 0 AND height > 0", name="ck_topology_notes_size_positive"),
    )
    op.create_index("ix_topology_notes_project_id", "topology_notes", ["project_id"])


def downgrade():
    op.drop_index("ix_topology_notes_project_id", table_name="topology_notes")
    op.drop_table("topology_notes")
