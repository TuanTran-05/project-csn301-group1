"""add chat sessions

Revision ID: 1d6734caee3b
Revises: 9bae573911ac
Create Date: 2026-08-02 00:00:00.000000

"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1d6734caee3b'
down_revision = '9bae573911ac'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'chat_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('chat_sessions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_chat_sessions_created_at'), ['created_at'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_chat_sessions_created_by_id'), ['created_by_id'], unique=False
        )

    # session_id starts nullable so existing rows can be backfilled below; it
    # is made NOT NULL once every row has a value.
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('session_id', sa.Integer(), nullable=True))

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    # .lastrowid is SQLite-specific, matching this project's only supported
    # database backend (see config.py).
    migration_session_id = bind.execute(
        sa.text(
            "INSERT INTO chat_sessions (created_by_id, created_at) "
            "VALUES (NULL, :created_at)"
        ).bindparams(created_at=now)
    ).lastrowid
    bind.execute(
        sa.text(
            "UPDATE chat_messages SET session_id = :session_id "
            "WHERE session_id IS NULL"
        ).bindparams(session_id=migration_session_id)
    )

    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.alter_column('session_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(
            batch_op.f('ix_chat_messages_session_id'), ['session_id'], unique=False
        )
        batch_op.create_foreign_key(
            'fk_chat_messages_session_id_chat_sessions',
            'chat_sessions',
            ['session_id'],
            ['id'],
            ondelete='CASCADE',
        )


def downgrade():
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.drop_constraint(
            'fk_chat_messages_session_id_chat_sessions', type_='foreignkey'
        )
        batch_op.drop_index(batch_op.f('ix_chat_messages_session_id'))
        batch_op.drop_column('session_id')

    with op.batch_alter_table('chat_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_sessions_created_by_id'))
        batch_op.drop_index(batch_op.f('ix_chat_sessions_created_at'))
    op.drop_table('chat_sessions')
