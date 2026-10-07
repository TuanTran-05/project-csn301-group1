"""Set (or update) the SSH credential for a single device.

Use this instead of re-running seed_lab.py when one device needs a
different username/password than the rest of the lab: seed_lab.py's
seed_credentials() applies one LAB_SSH_USERNAME/LAB_SSH_PASSWORD to every
device, so re-running it would overwrite a device-specific override.

Usage:
    python scripts/set_device_credential.py <hostname> <username> [project-name]

A project name is only needed when the hostname exists in several projects.

The password (and an optional enable secret) are prompted for without
echoing to the terminal and are never printed, logged, or passed as a
command-line argument or environment variable.
"""

import sys
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from network_copilot.app import create_app  # noqa: E402
from network_copilot.credentials.service import store_device_credential  # noqa: E402
from network_copilot.devices.service import get_device_by_hostname  # noqa: E402
from network_copilot.errors import NotFoundError  # noqa: E402


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(
            f"Usage: python {sys.argv[0]} <hostname> <username> [project-name]",
            file=sys.stderr,
        )
        return 1

    hostname, username = sys.argv[1], sys.argv[2]
    project_name = sys.argv[3] if len(sys.argv) == 4 else None

    password = getpass(f"SSH password for {username}@{hostname}: ")
    if not password:
        print("ERROR: password cannot be empty.", file=sys.stderr)
        return 1

    enable_secret = getpass("Enable secret (optional, press Enter to skip): ") or None

    app = create_app()
    with app.app_context():
        project_id = None
        if project_name:
            from network_copilot.extensions import db
            from network_copilot.projects.model import Project

            project = db.session.query(Project).filter_by(name=project_name).first()
            if project is None:
                print(f"ERROR: no project named '{project_name}'.", file=sys.stderr)
                return 1
            project_id = project.id
        try:
            device = get_device_by_hostname(hostname, project_id)
        except NotFoundError:
            print(
                f"ERROR: no device named '{hostname}' is in the inventory.",
                file=sys.stderr,
            )
            return 1

        store_device_credential(device.id, username, password, enable_secret)
        print(f"Credential updated for {hostname}: username={username}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
