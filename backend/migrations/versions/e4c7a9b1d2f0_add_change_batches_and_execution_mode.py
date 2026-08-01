"""Add change_batches table and execution_mode to change_requests

Revision ID: e4c7a9b1d2f0
Revises: b2ace2e71682
Create Date: 2026-08-01

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e4c7a9b1d2f0"
down_revision = "b2ace2e71682"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "change_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("requires_confirmation", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("description", sa.String(255)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("requested_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("approved_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("applied_at", sa.DateTime()),
    )
    op.create_index("ix_change_batches_created_at", "change_batches", ["created_at"])
    op.create_index("ix_change_batches_requested_by_id", "change_batches", ["requested_by_id"])
    with op.batch_alter_table("change_requests") as batch_op:
        batch_op.add_column(sa.Column("batch_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("execution_mode", sa.String(16), server_default="config", nullable=False))
        batch_op.create_foreign_key("fk_change_requests_batch_id", "change_batches", ["batch_id"], ["id"], ondelete="CASCADE")
        batch_op.create_index("ix_change_requests_batch_id", ["batch_id"])


def downgrade():
    with op.batch_alter_table("change_requests") as batch_op:
        batch_op.drop_index("ix_change_requests_batch_id")
        batch_op.drop_constraint("fk_change_requests_batch_id", type_="foreignkey")
        batch_op.drop_column("execution_mode")
        batch_op.drop_column("batch_id")
    op.drop_index("ix_change_batches_requested_by_id", table_name="change_batches")
    op.drop_index("ix_change_batches_created_at", table_name="change_batches")
    op.drop_table("change_batches")
