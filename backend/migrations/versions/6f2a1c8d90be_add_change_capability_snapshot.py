"""add frozen capability and verification metadata to change requests

Revision ID: 6f2a1c8d90be
Revises: 1d6734caee3b
Create Date: 2026-08-03

"""

from alembic import op
import sqlalchemy as sa


revision = "6f2a1c8d90be"
down_revision = "1d6734caee3b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("change_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("capability_tier", sa.String(32), nullable=True))
        batch_op.add_column(
            sa.Column("verification_level", sa.String(32), nullable=True)
        )
        batch_op.add_column(sa.Column("operation_families", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("operation_expectations", sa.JSON(), nullable=True)
        )
        batch_op.add_column(sa.Column("verification_plan", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("rollback_guidance", sa.JSON(), nullable=True))

    op.execute(
        "UPDATE change_requests SET capability_tier = 'best_effort' "
        "WHERE capability_tier IS NULL"
    )
    op.execute(
        "UPDATE change_requests SET verification_level = 'best_effort' "
        "WHERE verification_level IS NULL"
    )
    for column in (
        "operation_families",
        "operation_expectations",
        "verification_plan",
        "rollback_guidance",
    ):
        op.execute(
            f"UPDATE change_requests SET {column} = '[]' WHERE {column} IS NULL"
        )

    with op.batch_alter_table("change_requests", schema=None) as batch_op:
        batch_op.alter_column(
            "capability_tier",
            existing_type=sa.String(32),
            nullable=False,
            server_default="best_effort",
        )
        batch_op.alter_column(
            "verification_level",
            existing_type=sa.String(32),
            nullable=False,
            server_default="best_effort",
        )
        for column in (
            "operation_families",
            "operation_expectations",
            "verification_plan",
            "rollback_guidance",
        ):
            batch_op.alter_column(column, existing_type=sa.JSON(), nullable=False)


def downgrade():
    with op.batch_alter_table("change_requests", schema=None) as batch_op:
        batch_op.drop_column("rollback_guidance")
        batch_op.drop_column("verification_plan")
        batch_op.drop_column("operation_expectations")
        batch_op.drop_column("operation_families")
        batch_op.drop_column("verification_level")
        batch_op.drop_column("capability_tier")
