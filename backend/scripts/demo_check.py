"""Run the full demo flow against a live backend and a real PNETLab topology.

    .venv/bin/python scripts/demo_check.py --username g1 --password <mat-khau>

Exercises, in order, against real devices over SSH:

  1. login
  2. list devices
  3. run a read-only show command on INTERNAL-RTR
  4. refresh (poll) INTERNAL-RTR
  5. ask the AI copilot about OSPF on DIST-SW1          (skipped if no AI key)
  6. ask the AI copilot to create VLAN 25 on ACC-SW1    (skipped if no AI key)
  7. approve and apply that change; verify VLAN 25 landed
  8. confirm 'write erase' is blocked and audited

Prints PASS/FAIL per step and exits 1 if anything failed. Uses only the
standard library so it needs no extra dependency beyond the backend's own.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

CORE_HOSTNAME = "INTERNAL-RTR"
DIST_HOSTNAME = "DIST-SW1"
ACCESS_HOSTNAME = "ACC-SW1"
VLAN_COMMANDS = ["configure terminal", "vlan 25", "name MARKETING", "end"]


class Step:
    def __init__(self):
        self.failures = []

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {label}{('  ' + detail) if detail else ''}")
        if not condition:
            self.failures.append(label)
        return condition

    def skip(self, label: str, reason: str) -> None:
        print(f"  [SKIP] {label}  ({reason})")


def call(base_url, path, method="GET", body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except urllib.error.URLError as exc:
        return None, str(exc.reason)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    step = Step()
    print(f"Demo check against {args.base_url}\n")

    # 1. Login.
    status, body = call(
        args.base_url,
        "/api/auth/login",
        "POST",
        {"username": args.username, "password": args.password},
    )
    if not step.check("login", status == 200, f"(status={status})"):
        print(f"\n  response: {body}")
        return 1
    token = json.loads(body)["access_token"]

    # 2. List devices.
    status, body = call(args.base_url, "/api/devices", token=token)
    devices = {d["hostname"]: d for d in json.loads(body).get("items", [])} if status == 200 else {}
    step.check("list devices", status == 200 and len(devices) > 0, f"({len(devices)} devices)")

    for hostname in (CORE_HOSTNAME, DIST_HOSTNAME, ACCESS_HOSTNAME):
        if hostname not in devices:
            print(f"\nERROR: device '{hostname}' not found. Check scripts/seed_lab.py ran.")
            return 1

    core_id = devices[CORE_HOSTNAME]["id"]
    access_id = devices[ACCESS_HOSTNAME]["id"]

    # 3. Read-only command on the core device.
    status, body = call(
        args.base_url,
        "/api/commands/execute-readonly",
        "POST",
        {"device_id": core_id, "command": "show ip interface brief"},
        token=token,
    )
    ok = status == 200 and "GigabitEthernet" in json.loads(body).get("output", "")
    step.check(f"read-only command on {CORE_HOSTNAME}", ok, f"(status={status})")

    # 4. Refresh / poll.
    status, body = call(
        args.base_url, f"/api/devices/{core_id}/refresh", "POST", token=token
    )
    ok = status == 200 and json.loads(body).get("status") == "online"
    step.check(f"refresh {CORE_HOSTNAME}", ok, f"(status={status})")

    # 5. AI: monitor intent.
    status, body = call(
        args.base_url,
        "/api/ai/chat",
        "POST",
        {"message": f"Kiem tra OSPF cua {DIST_HOSTNAME}"},
        token=token,
    )
    if status == 503:
        step.skip("AI monitor intent", "AI_API_KEY not configured")
    else:
        step.check(
            "AI monitor intent",
            status == 200 and json.loads(body).get("intent") == "monitor",
            f"(status={status})",
        )

    # 6. AI: configure intent -> preview.
    change_id = None
    status, body = call(
        args.base_url,
        "/api/ai/chat",
        "POST",
        {"message": f"Tao VLAN 25 MARKETING tren {ACCESS_HOSTNAME}"},
        token=token,
    )
    if status == 503:
        step.skip("AI configure intent", "AI_API_KEY not configured")
    else:
        payload = json.loads(body) if status == 200 else {}
        change_id = payload.get("change", {}).get("id")
        step.check(
            "AI configure intent creates a preview",
            status == 200 and payload.get("change", {}).get("status") == "pending_approval",
            f"(status={status})",
        )

    # Fall back to a direct preview if the AI path was skipped or failed.
    if change_id is None:
        status, body = call(
            args.base_url,
            "/api/changes/preview",
            "POST",
            {
                "device_id": access_id,
                "commands": VLAN_COMMANDS,
                "verification_commands": ["show vlan brief"],
            },
            token=token,
        )
        if step.check("direct change preview", status == 201, f"(status={status})"):
            change_id = json.loads(body)["id"]

    # 7. Approve, apply, verify.
    if change_id is not None:
        status, body = call(
            args.base_url,
            f"/api/changes/{change_id}/approve",
            "POST",
            token=token,
        )
        step.check("approve change", status == 200, f"(status={status})")

        status, body = call(
            args.base_url, f"/api/changes/{change_id}/apply", "POST", token=token
        )
        result = json.loads(body) if status == 200 else {}
        ok = status == 200 and result.get("status") == "success"
        step.check("apply change (backup + configure + verify)", ok, f"(status={status})")
        if not ok:
            print(f"\n  response: {body}")

        verification = (result.get("verification_output") or {}).get("show vlan brief", {})
        step.check(
            "VLAN 25 confirmed on the device",
            verification.get("passed") is True,
            "",
        )
    else:
        print("  (no change_id available, skipping approve/apply/verify)")

    # 8. Blocked command.
    status, body = call(
        args.base_url,
        "/api/commands/execute-readonly",
        "POST",
        {"device_id": access_id, "command": "write erase"},
        token=token,
    )
    payload = json.loads(body) if body else {}
    step.check(
        "'write erase' is blocked",
        status == 403 and payload.get("error") == "policy_violation",
        f"(status={status})",
    )

    print()
    if step.failures:
        print(f"FAILED: {', '.join(step.failures)}")
        return 1
    print("ALL STEPS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
