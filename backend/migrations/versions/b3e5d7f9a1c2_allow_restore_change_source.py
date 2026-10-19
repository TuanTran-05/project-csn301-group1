"""Allow "restore" as a change source.

Restore previews are created with source="restore", which the CHECK
constraint added in c9b5e7a2d4f8 rejected, so every restore preview failed
with an IntegrityError.

Revision ID: b3e5d7f9a1c2
Revises: a8f4c2d9106b
"""

from alembic import op


revision = "b3e5d7f9a1c2"
down_revision = "a8f4c2d9106b"
branch_labels = None
depends_on = None


TABLES = ("change_requests", "change_batches")


def _replace_source_check(values):
    quoted = ", ".join(f"'{value}'" for value in values)
    for table in TABLES:
        name = f"ck_{table}_source_valid"
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(name, type_="check")
            batch_op.create_check_constraint(name, f"source IN ({quoted})")


def upgrade():
    _replace_source_check(("api", "ai", "design", "restore"))


def downgrade():
    op.execute("DELETE FROM change_requests WHERE source = 'restore'")
    op.execute("DELETE FROM change_batches WHERE source = 'restore'")
    _replace_source_check(("api", "ai", "design"))
