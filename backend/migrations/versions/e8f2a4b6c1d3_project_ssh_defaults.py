"""Default SSH login per project (project_ssh_defaults)

Revision ID: e8f2a4b6c1d3
Revises: d7e1f3a5b9c2
Create Date: 2026-10-28

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e8f2a4b6c1d3"
down_revision = "d7e1f3a5b9c2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_ssh_defaults",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE",
                                name="fk_project_ssh_defaults_project_id_projects"), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("enable_secret_encrypted", sa.Text()),
        sa.Column("updated_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL",
                                name="fk_project_ssh_defaults_updated_by_id_users")),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_project_ssh_defaults_project_id", "project_ssh_defaults", ["project_id"], unique=True)


def downgrade():
    op.drop_index("ix_project_ssh_defaults_project_id", table_name="project_ssh_defaults")
    op.drop_table("project_ssh_defaults")
