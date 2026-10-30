"""Remembered PNETLab login per project (project_pnetlab_logins)

Revision ID: f9a1b3c5d7e2
Revises: e8f2a4b6c1d3
Create Date: 2026-10-28

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f9a1b3c5d7e2"
down_revision = "e8f2a4b6c1d3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_pnetlab_logins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE",
                                name="fk_project_pnetlab_logins_project_id_projects"), nullable=False),
        sa.Column("url", sa.String(255), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("lab", sa.String(255), nullable=False, server_default=""),
        sa.Column("api_mode", sa.String(8), nullable=False, server_default="auto"),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL",
                                name="fk_project_pnetlab_logins_updated_by_id_users")),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_project_pnetlab_logins_project_id", "project_pnetlab_logins", ["project_id"], unique=True)


def downgrade():
    op.drop_index("ix_project_pnetlab_logins_project_id", table_name="project_pnetlab_logins")
    op.drop_table("project_pnetlab_logins")
