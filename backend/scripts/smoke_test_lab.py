"""Verify real SSH reachability against every seeded lab device.

Run this on the AI Server, which sits on the 10.10.10.0/24 management network:

    python scripts/smoke_test_lab.py

For each device it checks TCP/22, opens an SSH session and runs `show clock`.
Exits 1 if any device fails, so it can gate a demo or a CI job.

Credentials come from LAB_SSH_USERNAME / LAB_SSH_PASSWORD, or from the encrypted
store when those are not set. Nothing secret is ever printed.
"""

import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from network_copilot.app import create_app  # noqa: E402
from network_copilot.credentials.service import get_device_credential  # noqa: E402
from network_copilot.devices.model import Device  # noqa: E402
from network_copilot.errors import AppError  # noqa: E402
from network_copilot.extensions import db  # noqa: E402
from network_copilot.ssh.client import SSHClient  # noqa: E402
from network_copilot.ssh.types import SSHTarget  # noqa: E402

TCP_TIMEOUT = 5
PROBE_COMMAND = "show clock"


def tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return True
    except OSError:
        return False


def build_target(device: Device) -> SSHTarget | None:
    username = os.environ.get("LAB_SSH_USERNAME")
    password = os.environ.get("LAB_SSH_PASSWORD")

    if not username or not password:
        try:
            credential = get_device_credential(device.id)
        except AppError:
            return None
        username, password = credential.username, credential.password

    return SSHTarget(
        host=device.management_ip,
        port=device.ssh_port,
        username=username,
        password=password,
    )


def check(device: Device) -> tuple[bool, str]:
    if not tcp_open(device.management_ip, device.ssh_port):
        return False, f"TCP/{device.ssh_port} closed"

    target = build_target(device)
    if target is None:
        return False, "no credential available"

    try:
        client = SSHClient(target)
        result = client.run_show(PROBE_COMMAND)
    except AppError as exc:
        return False, exc.message
    except Exception as exc:  # pragma: no cover - unexpected transport failure
        return False, type(exc).__name__

    if not result.output.strip():
        return False, f"'{PROBE_COMMAND}' returned no output"
    return True, result.output.strip().splitlines()[0]


def main() -> int:
    app = create_app()
    with app.app_context():
        devices = db.session.query(Device).order_by(Device.hostname).all()

        if not devices:
            print("No devices found. Run scripts/seed_lab.py first.", file=sys.stderr)
            return 1

        print(f"Smoke testing {len(devices)} device(s) over SSH...\n")
        failures = []

        for device in devices:
            ok, detail = check(device)
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {device.hostname:<10} {device.management_ip:<14} {detail}")
            if not ok:
                failures.append(device.hostname)

        print()
        if failures:
            print(f"{len(failures)} device(s) failed: {', '.join(failures)}")
            return 1

        print("All devices reachable.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
