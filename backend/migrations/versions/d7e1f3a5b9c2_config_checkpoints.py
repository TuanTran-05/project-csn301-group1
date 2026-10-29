"""Named undo/redo steps of applied configuration (config_checkpoints)

Revision ID: d7e1f3a5b9c2
Revises: c5d7e9f1a3b4
Create Date: 2026-10-28

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d7e1f3a5b9c2"
down_revision = "c5d7e9f1a3b4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "config_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE",
                                name="fk_config_checkpoints_project_id_projects"), nullable=False),
        sa.Column("device_id", sa.Integer(),
                  sa.ForeignKey("devices.id", ondelete="CASCADE",
                                name="fk_config_checkpoints_device_id_devices"), nullable=False),
        sa.Column("change_request_id", sa.Integer(),
                  sa.ForeignKey("change_requests.id", ondelete="SET NULL",
                                name="fk_config_checkpoints_change_request_id_change_requests")),
        sa.Column("batch_id", sa.Integer(),
                  sa.ForeignKey("change_batches.id", ondelete="SET NULL",
                                name="fk_config_checkpoints_batch_id_change_batches")),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("commands", sa.JSON(), nullable=False),
        sa.Column("pre_backup_id", sa.Integer(),
                  sa.ForeignKey("config_backups.id", ondelete="SET NULL",
                                name="fk_config_checkpoints_pre_backup_id_config_backups")),
        sa.Column("post_backup_id", sa.Integer(),
                  sa.ForeignKey("config_backups.id", ondelete="SET NULL",
                                name="fk_config_checkpoints_post_backup_id_config_backups")),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("pending_change_id", sa.Integer(),
                  sa.ForeignKey("change_requests.id", ondelete="SET NULL",
                                name="fk_config_checkpoints_pending_change_id_change_requests")),
        sa.Column("pending_action", sa.String(8)),
        sa.Column("last_result", sa.String(255)),
        sa.Column("created_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL",
                                name="fk_config_checkpoints_created_by_id_users")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("undone_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('active', 'undone', 'discarded')",
                           name="ck_config_checkpoints_state_valid"),
        sa.CheckConstraint("pending_action IS NULL OR pending_action IN ('undo', 'redo')",
                           name="ck_config_checkpoints_pending_action_valid"),
    )
    op.create_index("ix_config_checkpoints_project_id", "config_checkpoints", ["project_id"])
    op.create_index("ix_config_checkpoints_device_id", "config_checkpoints", ["device_id"])
    op.create_index("ix_config_checkpoints_change_request_id", "config_checkpoints", ["change_request_id"])
    op.create_index("ix_config_checkpoints_created_at", "config_checkpoints", ["created_at"])
    op.create_index("ix_config_checkpoints_device_state", "config_checkpoints", ["device_id", "state"])


def downgrade():
    op.drop_index("ix_config_checkpoints_device_state", table_name="config_checkpoints")
    op.drop_index("ix_config_checkpoints_created_at", table_name="config_checkpoints")
    op.drop_index("ix_config_checkpoints_change_request_id", table_name="config_checkpoints")
    op.drop_index("ix_config_checkpoints_device_id", table_name="config_checkpoints")
    op.drop_index("ix_config_checkpoints_project_id", table_name="config_checkpoints")
    op.drop_table("config_checkpoints")
