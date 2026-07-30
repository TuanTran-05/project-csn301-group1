"""Seed the PNETLab topology into the backend database.

Usage:
    python scripts/seed_lab.py

Environment:
    SEED_ADMIN_USERNAME   defaults to "admin"
    SEED_ADMIN_PASSWORD   required, used for the initial ADMIN account
    LAB_SSH_USERNAME      optional, stored (encrypted) for every device
    LAB_SSH_PASSWORD      optional, stored (encrypted) for every device

The script is idempotent: running it twice updates the existing rows rather
than creating duplicates. No secret is ever printed.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from network_copilot.app import create_app  # noqa: E402
from network_copilot.auth.model import User  # noqa: E402
from network_copilot.credentials.service import (  # noqa: E402
    store_device_credential,
)
from network_copilot.devices.model import Device  # noqa: E402
from network_copilot.extensions import db  # noqa: E402

# Names must match the device hostnames in PNETLab exactly: the AI copilot
# resolves a device by hostname, so a mismatch is a hard failure.
# INTERNAL-RTR is a router that fills the core role in this topology.
LAB_DEVICES = [
    ("ISP-RTR", "10.10.10.4", "cisco_ios", "isp"),
    ("FW-01", "10.10.10.3", "cisco_asa", "firewall"),
    ("INTERNAL-RTR", "10.10.10.11", "cisco_ios", "core"),
    ("DIST-SW1", "10.10.10.21", "cisco_ios", "distribution"),
    ("DIST-SW2", "10.10.10.22", "cisco_ios", "distribution"),
    ("ACC-SW1", "10.10.10.31", "cisco_ios", "access"),
    ("ACC-SW3", "10.10.10.33", "cisco_ios", "access"),
    ("DMZ-SW", "10.10.10.34", "cisco_ios", "dmz"),
]


def seed_admin() -> int:
    username = os.environ.get("SEED_ADMIN_USERNAME", "admin")
    password = os.environ.get("SEED_ADMIN_PASSWORD")

    user = db.session.query(User).filter_by(username=username).one_or_none()
    if user is not None:
        print(f"  admin user '{username}' already exists")
        return 0

    if not password:
        print(
            "ERROR: SEED_ADMIN_PASSWORD is not set. Export it before seeding so the "
            "initial admin account has a password you chose.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    user = User(username=username, role="ADMIN")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    print(f"  created ADMIN user '{username}'")
    return 1


def seed_devices() -> tuple[int, int]:
    created = updated = 0
    for hostname, ip, device_type, role in LAB_DEVICES:
        device = db.session.query(Device).filter_by(hostname=hostname).one_or_none()
        if device is None:
            device = Device(hostname=hostname)
            db.session.add(device)
            created += 1
        else:
            updated += 1

        device.management_ip = ip
        device.device_type = device_type
        device.role = role
        device.ssh_port = 22
        device.monitoring_enabled = True
        if device.status is None:
            device.status = "unknown"

    db.session.commit()
    return created, updated


def seed_credentials() -> int:
    username = os.environ.get("LAB_SSH_USERNAME")
    password = os.environ.get("LAB_SSH_PASSWORD")
    if not username or not password:
        print(
            "  LAB_SSH_USERNAME / LAB_SSH_PASSWORD not set: skipping credentials. "
            "SSH features will not work until they are stored."
        )
        return 0

    count = 0
    for device in db.session.query(Device).all():
        store_device_credential(device.id, username, password)
        count += 1
    return count


def main() -> int:
    app = create_app()
    with app.app_context():
        print("Seeding lab inventory...")
        seed_admin()
        created, updated = seed_devices()
        print(f"  devices: {created} created, {updated} updated")
        stored = seed_credentials()
        if stored:
            print(f"  credentials stored (encrypted) for {stored} device(s)")
        print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
