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


def _columns(table):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # SQLite cannot ALTER a table to add a foreign key, so every change goes
    # through batch mode. The first release of this migration used plain
    # add_column and failed half-way on SQLite, leaving some columns behind
    # without bumping alembic_version; skip whatever already exists.
    if "ai_management_enabled" not in _columns("projects"):
        with op.batch_alter_table("projects") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "ai_management_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
    if "ai_management_approved" not in _columns("change_requests"):
        with op.batch_alter_table("change_requests") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "ai_management_approved",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
    has_chat_message = "chat_message_id" in _columns("change_batches")
    with op.batch_alter_table("change_batches", recreate="always") as batch_op:
        if not has_chat_message:
            batch_op.add_column(sa.Column("chat_message_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_change_batches_chat_message_id",
            "chat_messages",
            ["chat_message_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "uq_change_batches_chat_message", ["chat_message_id"], unique=True
        )


def downgrade():
    with op.batch_alter_table("change_batches") as batch_op:
        batch_op.drop_index("uq_change_batches_chat_message")
        batch_op.drop_constraint("fk_change_batches_chat_message_id", type_="foreignkey")
        batch_op.drop_column("chat_message_id")
    with op.batch_alter_table("change_requests") as batch_op:
        batch_op.drop_column("ai_management_approved")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("ai_management_enabled")
