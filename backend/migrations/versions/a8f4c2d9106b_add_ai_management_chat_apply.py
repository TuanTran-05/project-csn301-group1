"""Add owner-gated AI Management for explicit chat configuration apply.

Revision ID: a8f4c2d9106b
Revises: 7b8d9e0f1a2c
"""

from alembic import op
import sqlalchemy as sa


revision = "a8f4c2d9106b"
down_revision = "7b8d9e0f1a2c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projects",
        sa.Column(
            "ai_management_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "change_requests",
        sa.Column(
            "ai_management_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "change_batches",
        sa.Column(
            "chat_message_id",
            sa.Integer(),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_change_batches_chat_message",
        "change_batches",
        ["chat_message_id"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_change_batches_chat_message", table_name="change_batches")
    op.drop_column("change_batches", "chat_message_id")
    op.drop_column("change_requests", "ai_management_approved")
    op.drop_column("projects", "ai_management_enabled")
