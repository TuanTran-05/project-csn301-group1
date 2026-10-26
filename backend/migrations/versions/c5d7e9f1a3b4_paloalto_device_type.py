"""Palo Alto PAN-OS firewalls: device_type gains "paloalto_panos"

Revision ID: c5d7e9f1a3b4
Revises: b3e5d7f9a1c2
Create Date: 2026-10-26

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c5d7e9f1a3b4"
down_revision = "b3e5d7f9a1c2"
branch_labels = None
depends_on = None


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


OLD_TYPES = ("cisco_ios", "cisco_asa", "generic")
NEW_TYPES = ("cisco_ios", "cisco_asa", "paloalto_panos", "generic")


def upgrade():
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_constraint("ck_devices_device_type_valid", type_="check")
        batch_op.create_check_constraint("ck_devices_device_type_valid", _in("device_type", NEW_TYPES))


def downgrade():
    # The old schema has no place for a PAN-OS firewall.
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM devices WHERE device_type = 'paloalto_panos'"))
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_constraint("ck_devices_device_type_valid", type_="check")
        batch_op.create_check_constraint("ck_devices_device_type_valid", _in("device_type", OLD_TYPES))
