"""Device kinds, unmanaged devices and the planned/provisioned lifecycle

* devices.kind / managed / lifecycle / data_ip / data_gateway
* management_ip becomes optional (unmanaged and planned devices have none)
* device_type gains "generic", role gains host/server/internet/wireless/segment
* projects.addressing_pool: the block link and VLAN subnets are carved from

Revision ID: e5f9a3b7c2d1
Revises: d4e8f2a6b1c9
Create Date: 2026-10-12

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e5f9a3b7c2d1"
down_revision = "d4e8f2a6b1c9"
branch_labels = None
depends_on = None


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


OLD_TYPES = ("cisco_ios", "cisco_asa")
NEW_TYPES = OLD_TYPES + ("generic",)
OLD_ROLES = ("isp", "firewall", "core", "distribution", "access", "dmz", "management")
NEW_ROLES = OLD_ROLES + ("host", "server", "internet", "wireless", "segment")
KINDS = ("router", "l3_switch", "switch", "firewall", "pc", "server", "cloud", "ap", "segment")


def upgrade():
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_constraint("ck_devices_device_type_valid", type_="check")
        batch_op.drop_constraint("ck_devices_role_valid", type_="check")
        batch_op.add_column(sa.Column("kind", sa.String(16), server_default="router", nullable=False))
        batch_op.add_column(sa.Column("managed", sa.Boolean(), server_default=sa.true(), nullable=False))
        batch_op.add_column(sa.Column("lifecycle", sa.String(12), server_default="provisioned", nullable=False))
        batch_op.add_column(sa.Column("data_ip", sa.String(43)))
        batch_op.add_column(sa.Column("data_gateway", sa.String(45)))
        batch_op.alter_column("management_ip", existing_type=sa.String(45), nullable=True)

    # Existing devices are all managed Cisco devices: give them the kind that
    # matches their role.
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE devices SET kind = 'router' WHERE role = 'core'"))
    bind.execute(sa.text("UPDATE devices SET kind = 'l3_switch' WHERE role = 'distribution'"))
    bind.execute(sa.text("UPDATE devices SET kind = 'firewall' WHERE role = 'firewall' OR device_type = 'cisco_asa'"))
    bind.execute(sa.text("UPDATE devices SET kind = 'switch' WHERE role IN ('access', 'dmz', 'management') AND device_type = 'cisco_ios'"))
    bind.execute(sa.text("UPDATE devices SET kind = 'router' WHERE role = 'isp'"))

    with op.batch_alter_table("devices") as batch_op:
        batch_op.create_check_constraint("ck_devices_device_type_valid", _in("device_type", NEW_TYPES))
        batch_op.create_check_constraint("ck_devices_role_valid", _in("role", NEW_ROLES))
        batch_op.create_check_constraint("ck_devices_kind_valid", _in("kind", KINDS))
        batch_op.create_check_constraint("ck_devices_lifecycle_valid", _in("lifecycle", ("planned", "provisioned")))
        batch_op.create_check_constraint("ck_devices_managed_matches_type", "(managed = 1) = (device_type <> 'generic')")
        batch_op.create_check_constraint(
            "ck_devices_provisioned_has_ip",
            "managed = 0 OR lifecycle = 'planned' OR management_ip IS NOT NULL",
        )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("addressing_pool", sa.String(43)))


def downgrade():
    # Unmanaged devices and planned ones have no place in the old schema.
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM devices WHERE managed = 0 OR management_ip IS NULL"))
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("addressing_pool")
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_constraint("ck_devices_provisioned_has_ip", type_="check")
        batch_op.drop_constraint("ck_devices_managed_matches_type", type_="check")
        batch_op.drop_constraint("ck_devices_lifecycle_valid", type_="check")
        batch_op.drop_constraint("ck_devices_kind_valid", type_="check")
        batch_op.drop_constraint("ck_devices_role_valid", type_="check")
        batch_op.drop_constraint("ck_devices_device_type_valid", type_="check")
        batch_op.drop_column("data_gateway")
        batch_op.drop_column("data_ip")
        batch_op.drop_column("lifecycle")
        batch_op.drop_column("managed")
        batch_op.drop_column("kind")
        batch_op.alter_column("management_ip", existing_type=sa.String(45), nullable=False)
        batch_op.create_check_constraint("ck_devices_device_type_valid", _in("device_type", OLD_TYPES))
        batch_op.create_check_constraint("ck_devices_role_valid", _in("role", OLD_ROLES))
