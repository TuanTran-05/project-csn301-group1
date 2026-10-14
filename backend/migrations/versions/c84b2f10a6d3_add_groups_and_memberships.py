"""Add groups and role-based device memberships.

Revision ID: c84b2f10a6d3
Revises: 6f2a1c8d90be
Create Date: 2026-10-11
"""

from alembic import op
import sqlalchemy as sa


revision = "c84b2f10a6d3"
down_revision = "6f2a1c8d90be"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "is_auto_approve",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_groups_name", "groups", ["name"], unique=True)

    op.create_table(
        "teacher_groups",
        sa.Column("teacher_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["teacher_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("teacher_id", "group_id"),
    )
    op.create_table(
        "student_groups",
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("student_id", "group_id"),
    )
    op.create_table(
        "group_devices",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "device_id"),
    )


def downgrade():
    op.drop_table("group_devices")
    op.drop_table("student_groups")
    op.drop_table("teacher_groups")
    op.drop_index("ix_groups_name", table_name="groups")
    op.drop_table("groups")
