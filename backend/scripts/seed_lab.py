"""Seed the PNETLab topology into the backend database.

Usage:
    python scripts/seed_lab.py

Everything is created inside one project (the lab), owned by the admin.

Environment:
    SEED_PROJECT_NAME     defaults to "PNETLab"
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

from flask import current_app  # noqa: E402

from network_copilot.app import create_app  # noqa: E402
from network_copilot.auth.model import User  # noqa: E402
from network_copilot.credentials.service import (  # noqa: E402
    store_device_credential,
)
from network_copilot.devices.model import Device  # noqa: E402
from network_copilot.extensions import db  # noqa: E402
from network_copilot.projects.model import Project  # noqa: E402

# Names must match the device hostnames in PNETLab exactly: the AI copilot
# resolves a device by hostname, so a mismatch is a hard failure.
LAB_DEVICES = [
    ("R1", "172.16.3.111", "cisco_ios", "core"),
    ("SW1", "172.16.3.121", "cisco_ios", "access"),
    ("SW2", "172.16.3.122", "cisco_ios", "access"),
]


def seed_project() -> Project:
    """The project the lab devices live in; created on first run."""
    name = os.environ.get("SEED_PROJECT_NAME", "PNETLab")
    project = db.session.query(Project).filter_by(name=name).first()
    if project is None:
        owner = (
            db.session.query(User)
            .filter_by(username=os.environ.get("SEED_ADMIN_USERNAME", "admin"))
            .one_or_none()
        )
        project = Project(
            name=name,
            description="PNETLab Cisco lab",
            management_network=current_app.config["MANAGEMENT_NETWORK"],
            environment="pnetlab",
            owner_id=owner.id if owner else None,
        )
        db.session.add(project)
        db.session.commit()
        print(f"  created project '{name}'")
    return project


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


def seed_devices(project: Project | None = None) -> tuple[int, int]:
    project = project or seed_project()
    created = updated = 0
    for hostname, ip, device_type, role in LAB_DEVICES:
        device = (
            db.session.query(Device)
            .filter_by(project_id=project.id, hostname=hostname)
            .one_or_none()
        )
        if device is None:
            device = Device(project_id=project.id, hostname=hostname)
            db.session.add(device)
            created += 1
        else:
            updated += 1

        device.management_ip = ip
        device.device_type = device_type
        device.role = role
        device.ssh_port = 22
        device.monitoring_enabled = True
        device.environment = "pnetlab"
        if device.status is None:
            device.status = "unknown"

    db.session.commit()
    return created, updated


def seed_credentials(project: Project | None = None) -> int:
    username = os.environ.get("LAB_SSH_USERNAME")
    password = os.environ.get("LAB_SSH_PASSWORD")
    if not username or not password:
        print(
            "  LAB_SSH_USERNAME / LAB_SSH_PASSWORD not set: skipping credentials. "
            "SSH features will not work until they are stored."
        )
        return 0

    count = 0
    project = project or seed_project()
    for device in db.session.query(Device).filter_by(project_id=project.id):
        store_device_credential(device.id, username, password)
        count += 1
    return count


def main() -> int:
    app = create_app()
    with app.app_context():
        print("Seeding lab inventory...")
        seed_admin()
        project = seed_project()
        created, updated = seed_devices(project)
        print(f"  devices: {created} created, {updated} updated")
        stored = seed_credentials(project)
        if stored:
            print(f"  credentials stored (encrypted) for {stored} device(s)")
        print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
