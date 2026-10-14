"""Normalize the schema and add user-management columns

* users: full name, unique email, last login, token version (revokes tokens
  when a password / role / active flag changes), who created the account.
* CHECK constraints so the database rejects invalid enum values itself.
* Composite indexes for the queries the application actually runs.
* Topology links: one cable per interface, no link from a device to itself.
* Rows that reference a parent that no longer exists are cleaned up first,
  because FOREIGN KEY enforcement is switched on at runtime from now on.

Revision ID: c9b5e7a2d4f8
Revises: a7d3c1f09e42
Create Date: 2026-10-08

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c9b5e7a2d4f8"
down_revision = "a7d3c1f09e42"
branch_labels = None
depends_on = None


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# (table, constraint name, expression)
CHECKS = [
    ("users", "ck_users_role_valid", _in("role", ("ADMIN", "OPERATOR", "VIEWER"))),
    ("devices", "ck_devices_device_type_valid", _in("device_type", ("cisco_ios", "cisco_asa"))),
    ("devices", "ck_devices_role_valid", _in("role", (
        "isp", "firewall", "core", "distribution", "access", "dmz", "management"))),
    ("devices", "ck_devices_status_valid", _in("status", ("unknown", "online", "offline"))),
    ("devices", "ck_devices_environment_valid", _in("environment", ("pnetlab", "physical"))),
    ("devices", "ck_devices_ssh_port_range", "ssh_port BETWEEN 1 AND 65535"),
    ("projects", "ck_projects_environment_valid", _in("environment", ("pnetlab", "physical", "mixed"))),
    ("project_members", "ck_project_members_access_valid", _in("access", ("viewer", "editor"))),
    ("topology_links", "ck_topology_links_link_type_valid", _in("link_type", ("physical", "routed", "trunk"))),
    ("topology_links", "ck_topology_links_distinct_devices", "device_a_id <> device_b_id"),
    ("change_batches", "ck_change_batches_status_valid", _in("status", (
        "pending_approval", "approved", "running", "success", "partial_success", "failed", "cancelled"))),
    ("change_batches", "ck_change_batches_risk_level_valid", _in("risk_level", ("low", "medium", "high"))),
    ("change_batches", "ck_change_batches_source_valid", _in("source", ("api", "ai", "design"))),
    ("change_requests", "ck_change_requests_status_valid", _in("status", (
        "pending_approval", "approved", "running", "success", "failed", "cancelled"))),
    ("change_requests", "ck_change_requests_risk_level_valid", _in("risk_level", ("low", "medium", "high"))),
    ("change_requests", "ck_change_requests_source_valid", _in("source", ("api", "ai", "design"))),
    ("change_requests", "ck_change_requests_execution_mode_valid", _in("execution_mode", ("config", "exec"))),
    ("audit_logs", "ck_audit_logs_result_valid", _in("result", ("success", "failure", "blocked"))),
    ("command_executions", "ck_command_executions_status_valid", _in("status", ("success", "failed", "blocked"))),
    ("device_snapshots", "ck_device_snapshots_status_valid", _in("status", ("online", "offline"))),
    ("chat_messages", "ck_chat_messages_role_valid", _in("role", ("user", "assistant", "system"))),
]

# (index name, table, columns)
INDEXES = [
    ("ix_change_requests_device_status", "change_requests", ["device_id", "status"]),
    ("ix_audit_logs_project_created", "audit_logs", ["project_id", "created_at"]),
    ("ix_command_executions_device_created", "command_executions", ["device_id", "created_at"]),
    ("ix_device_snapshots_device_created", "device_snapshots", ["device_id", "created_at"]),
    ("ix_config_backups_device_created", "config_backups", ["device_id", "created_at"]),
    ("ix_chat_messages_session_created", "chat_messages", ["session_id", "created_at"]),
]


def _remove_orphans(bind):
    """Clear rows whose foreign key points at a row that no longer exists.

    Nullable references are set to NULL, mandatory ones lose the row, which is
    what ON DELETE SET NULL / CASCADE would have done had the database been
    enforcing them.
    """
    inspector = sa.inspect(bind)
    for table in inspector.get_table_names():
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        for fk in inspector.get_foreign_keys(table):
            if len(fk["constrained_columns"]) != 1:
                continue
            column = fk["constrained_columns"][0]
            parent = fk["referred_table"]
            parent_key = fk["referred_columns"][0]
            missing = (
                f"{column} IS NOT NULL AND {column} NOT IN "
                f"(SELECT {parent_key} FROM {parent})"
            )
            if columns[column]["nullable"]:
                bind.execute(sa.text(f"UPDATE {table} SET {column} = NULL WHERE {missing}"))
            else:
                bind.execute(sa.text(f"DELETE FROM {table} WHERE {missing}"))


def upgrade():
    bind = op.get_bind()
    _remove_orphans(bind)

    # -- users ----------------------------------------------------------
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("full_name", sa.String(120)))
        batch_op.add_column(sa.Column("email", sa.String(254)))
        batch_op.add_column(sa.Column("token_version", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime()))
        batch_op.add_column(sa.Column("password_changed_at", sa.DateTime()))
        batch_op.add_column(sa.Column("created_by_id", sa.Integer()))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime()))
    bind.execute(sa.text("UPDATE users SET updated_at = created_at"))
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)
        batch_op.create_unique_constraint("uq_users_email", ["email"])
        batch_op.create_foreign_key(
            "fk_users_created_by_id_users", "users", ["created_by_id"], ["id"], ondelete="SET NULL"
        )

    # -- topology links: one cable per interface -------------------------
    with op.batch_alter_table("topology_links") as batch_op:
        batch_op.create_unique_constraint("uq_topology_links_end_a", ["device_a_id", "interface_a"])
        batch_op.create_unique_constraint("uq_topology_links_end_b", ["device_b_id", "interface_b"])

    # -- CHECK constraints ------------------------------------------------
    for table, name, expression in CHECKS:
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_check_constraint(name, expression)

    # -- composite indexes ------------------------------------------------
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)

    for table, name, _expression in reversed(CHECKS):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(name, type_="check")

    with op.batch_alter_table("topology_links") as batch_op:
        batch_op.drop_constraint("uq_topology_links_end_b", type_="unique")
        batch_op.drop_constraint("uq_topology_links_end_a", type_="unique")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("fk_users_created_by_id_users", type_="foreignkey")
        batch_op.drop_constraint("uq_users_email", type_="unique")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_by_id")
        batch_op.drop_column("password_changed_at")
        batch_op.drop_column("last_login_at")
        batch_op.drop_column("token_version")
        batch_op.drop_column("email")
        batch_op.drop_column("full_name")
