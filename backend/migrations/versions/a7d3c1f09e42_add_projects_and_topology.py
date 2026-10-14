"""Add projects, project sharing and topology links

Every device, chat session and change batch now belongs to a project, and
hostname / management IP are unique per project instead of globally.
Existing rows are moved into a single "Default Lab" project so an upgraded
installation keeps working exactly as before.

Revision ID: a7d3c1f09e42
Revises: 6f2a1c8d90be
Create Date: 2026-10-07

"""
import os
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7d3c1f09e42"
down_revision = "6f2a1c8d90be"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_NAME = "Default Lab"


def _has_rows(bind, table: str) -> bool:
    return bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None


def upgrade():
    bind = op.get_bind()

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(255)),
        sa.Column("management_network", sa.String(43), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])

    op.create_table(
        "project_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access", sa.String(16), nullable=False),
        sa.Column("granted_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_members_user"),
    )
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])

    op.create_table(
        "topology_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_a_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interface_a", sa.String(40), nullable=False),
        sa.Column("device_b_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interface_b", sa.String(40), nullable=False),
        sa.Column("link_type", sa.String(16), nullable=False),
        sa.Column("network", sa.String(43)),
        sa.Column("ip_a", sa.String(45)),
        sa.Column("ip_b", sa.String(45)),
        sa.Column("allowed_vlans", sa.String(255)),
        sa.Column("bring_up", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("description", sa.String(255)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_topology_links_project_id", "topology_links", ["project_id"])

    # New columns start nullable so existing rows can be backfilled.
    with op.batch_alter_table("devices") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("environment", sa.String(16), server_default="pnetlab", nullable=False))
        batch_op.add_column(sa.Column("pos_x", sa.Float()))
        batch_op.add_column(sa.Column("pos_y", sa.Float()))
    with op.batch_alter_table("chat_sessions") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("change_batches") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.Integer(), nullable=True))

    # Move existing data into one project. An installation with no devices,
    # batches or chat messages is fresh: the only chat session it can hold is
    # the empty one an earlier migration creates, which is dropped so no
    # stray project is invented.
    if any(_has_rows(bind, t) for t in ("devices", "change_batches", "chat_messages")):
        owner = bind.execute(
            sa.text("SELECT id FROM users WHERE role = 'ADMIN' ORDER BY id LIMIT 1")
        ).scalar()
        now = datetime.now(timezone.utc)
        bind.execute(
            sa.text(
                "INSERT INTO projects (name, description, management_network, "
                "environment, owner_id, created_at, updated_at) VALUES "
                "(:name, :description, :network, 'pnetlab', :owner, :now, :now)"
            ),
            {
                "name": DEFAULT_PROJECT_NAME,
                "description": "Created from the data that existed before projects.",
                "network": os.environ.get("MANAGEMENT_NETWORK", "172.16.3.0/24"),
                "owner": owner,
                "now": now,
            },
        )
        project_id = bind.execute(
            sa.text("SELECT id FROM projects WHERE name = :name"),
            {"name": DEFAULT_PROJECT_NAME},
        ).scalar()
        # Everyone could see every device before, so keep that read access:
        # existing non-admin users become viewers of the default project
        # (ADMIN sees all projects anyway).
        bind.execute(
            sa.text(
                "INSERT INTO project_members "
                "(project_id, user_id, access, granted_by_id, created_at) "
                "SELECT :pid, id, 'viewer', NULL, :now FROM users "
                "WHERE role != 'ADMIN'"
            ),
            {"pid": project_id, "now": now},
        )
        for table in ("devices", "chat_sessions", "change_batches"):
            bind.execute(
                sa.text(f"UPDATE {table} SET project_id = :pid"), {"pid": project_id}
            )
        bind.execute(
            sa.text(
                "UPDATE audit_logs SET project_id = :pid "
                "WHERE device_id IS NOT NULL"
            ),
            {"pid": project_id},
        )

    else:
        bind.execute(sa.text("DELETE FROM chat_sessions"))

    # Constraints, now that every row has a project.
    with op.batch_alter_table("devices") as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key("fk_devices_project_id", "projects", ["project_id"], ["id"], ondelete="CASCADE")
        batch_op.create_index("ix_devices_project_id", ["project_id"])
        batch_op.drop_index("ix_devices_hostname")
        batch_op.drop_index("ix_devices_management_ip")
        batch_op.create_index("ix_devices_hostname", ["hostname"])
        batch_op.create_index("ix_devices_management_ip", ["management_ip"])
        batch_op.create_unique_constraint("uq_devices_project_hostname", ["project_id", "hostname"])
        batch_op.create_unique_constraint("uq_devices_project_management_ip", ["project_id", "management_ip"])
    with op.batch_alter_table("chat_sessions") as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key("fk_chat_sessions_project_id", "projects", ["project_id"], ["id"], ondelete="CASCADE")
        batch_op.create_index("ix_chat_sessions_project_id", ["project_id"])
    with op.batch_alter_table("change_batches") as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key("fk_change_batches_project_id", "projects", ["project_id"], ["id"], ondelete="CASCADE")
        batch_op.create_index("ix_change_batches_project_id", ["project_id"])
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.create_foreign_key("fk_audit_logs_project_id", "projects", ["project_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index("ix_audit_logs_project_id", ["project_id"])


def downgrade():
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_index("ix_audit_logs_project_id")
        batch_op.drop_constraint("fk_audit_logs_project_id", type_="foreignkey")
        batch_op.drop_column("project_id")
    with op.batch_alter_table("change_batches") as batch_op:
        batch_op.drop_index("ix_change_batches_project_id")
        batch_op.drop_constraint("fk_change_batches_project_id", type_="foreignkey")
        batch_op.drop_column("project_id")
    with op.batch_alter_table("chat_sessions") as batch_op:
        batch_op.drop_index("ix_chat_sessions_project_id")
        batch_op.drop_constraint("fk_chat_sessions_project_id", type_="foreignkey")
        batch_op.drop_column("project_id")
    # Restoring global uniqueness fails if two projects reuse a hostname or IP.
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_constraint("uq_devices_project_management_ip", type_="unique")
        batch_op.drop_constraint("uq_devices_project_hostname", type_="unique")
        batch_op.drop_index("ix_devices_management_ip")
        batch_op.drop_index("ix_devices_hostname")
        batch_op.create_index("ix_devices_hostname", ["hostname"], unique=True)
        batch_op.create_index("ix_devices_management_ip", ["management_ip"], unique=True)
        batch_op.drop_index("ix_devices_project_id")
        batch_op.drop_constraint("fk_devices_project_id", type_="foreignkey")
        batch_op.drop_column("pos_y")
        batch_op.drop_column("pos_x")
        batch_op.drop_column("environment")
        batch_op.drop_column("project_id")

    op.drop_index("ix_topology_links_project_id", table_name="topology_links")
    op.drop_table("topology_links")
    op.drop_index("ix_project_members_user_id", table_name="project_members")
    op.drop_index("ix_project_members_project_id", table_name="project_members")
    op.drop_table("project_members")
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_table("projects")
