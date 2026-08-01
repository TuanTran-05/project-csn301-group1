"""Freeze device connection identity onto change_requests

Revision ID: 9bae573911ac
Revises: e4c7a9b1d2f0
Create Date: 2026-08-02

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9bae573911ac"
down_revision = "e4c7a9b1d2f0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("change_requests") as batch_op:
        batch_op.add_column(sa.Column("target_hostname", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("target_management_ip", sa.String(45), nullable=True))
        batch_op.add_column(sa.Column("target_ssh_port", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("target_device_type", sa.String(32), nullable=True))

    # Backfill existing rows from their current device so Apply on
    # already-previewed changes still has an identity snapshot to compare
    # against.
    op.execute(
        """
        UPDATE change_requests
        SET target_hostname = (
                SELECT hostname FROM devices WHERE devices.id = change_requests.device_id
            ),
            target_management_ip = (
                SELECT management_ip FROM devices WHERE devices.id = change_requests.device_id
            ),
            target_ssh_port = (
                SELECT ssh_port FROM devices WHERE devices.id = change_requests.device_id
            ),
            target_device_type = (
                SELECT device_type FROM devices WHERE devices.id = change_requests.device_id
            )
        """
    )
    # Defensive fallback for any row whose device no longer exists.
    op.execute(
        "UPDATE change_requests SET target_hostname = 'UNKNOWN' WHERE target_hostname IS NULL"
    )
    op.execute(
        "UPDATE change_requests SET target_management_ip = '0.0.0.0' "
        "WHERE target_management_ip IS NULL"
    )
    op.execute(
        "UPDATE change_requests SET target_ssh_port = 22 WHERE target_ssh_port IS NULL"
    )
    op.execute(
        "UPDATE change_requests SET target_device_type = 'cisco_ios' "
        "WHERE target_device_type IS NULL"
    )

    with op.batch_alter_table("change_requests") as batch_op:
        batch_op.alter_column("target_hostname", existing_type=sa.String(64), nullable=False)
        batch_op.alter_column(
            "target_management_ip", existing_type=sa.String(45), nullable=False
        )
        batch_op.alter_column(
            "target_ssh_port",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="22",
        )
        batch_op.alter_column("target_device_type", existing_type=sa.String(32), nullable=False)


def downgrade():
    with op.batch_alter_table("change_requests") as batch_op:
        batch_op.drop_column("target_device_type")
        batch_op.drop_column("target_ssh_port")
        batch_op.drop_column("target_management_ip")
        batch_op.drop_column("target_hostname")
