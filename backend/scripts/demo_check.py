"""Run the full demo flow against a live backend and a real PNETLab topology.

    .venv/bin/python scripts/demo_check.py --username admin --password <chosen-password>

Exercises, in order, against real devices over SSH:

  1. login
  2. list devices
  3. run a read-only show command on INTERNAL-RTR
  4. refresh (poll) INTERNAL-RTR
  5. ask the AI copilot about OSPF on DIST-SW1          (skipped if no AI key)
  6. ask the AI copilot to write memory on every device
  7. inspect the frozen targets, execution mode, commands, risk, and confirmation
  8. approve the batch and apply it with the exact text CONFIRM ALL
  9. review every child result; partial success is reported for manual follow-up

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
WRITE_ALL_REQUEST = "thuc hien lenh write tren toan bo thiet bi"


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

    # 6. AI: configure intent -> frozen multi-device preview.
    status, body = call(
        args.base_url,
        "/api/ai/chat",
        "POST",
        {"message": WRITE_ALL_REQUEST},
        token=token,
    )
    payload = json.loads(body) if status == 200 else {}
    batch = payload.get("batch") or {}
    children = batch.get("changes") or []
    batch_id = batch.get("id")
    preview_ok = step.check(
        "AI write-all request creates a batch preview",
        status == 200 and batch.get("status") == "pending_approval",
        f"(status={status})",
    )

    # 7. Inspect exactly what was frozen before allowing the script to approve.
    frozen_hostnames = {
        child.get("device", {}).get("hostname") for child in children
    }
    inspections = [
        step.check(
            "preview freezes every current device",
            frozen_hostnames == set(devices),
            f"({len(frozen_hostnames)}/{len(devices)} devices)",
        ),
        step.check(
            "every child uses EXEC mode",
            bool(children)
            and all(child.get("execution_mode") == "exec" for child in children),
        ),
        step.check(
            "every child freezes 'write memory'",
            bool(children)
            and all(child.get("commands") == ["write memory"] for child in children),
        ),
        step.check(
            "batch is high risk and requires CONFIRM ALL",
            batch.get("risk_level") == "high"
            and batch.get("requires_confirmation") is True
            and batch.get("confirmation_text") == "CONFIRM ALL",
        ),
    ]

    for child in children:
        device = child.get("device", {}).get("hostname", "<unknown>")
        print(
            f"    frozen: {device} mode={child.get('execution_mode')} "
            f"commands={child.get('commands')} risk={child.get('risk_level')}"
        )

    # 8 & 9. Approve, type the exact confirmation, apply, and inspect every
    # child. Never continue when the preview differs from the requested scope.
    if preview_ok and all(inspections) and batch_id is not None:
        status, body = call(
            args.base_url,
            f"/api/change-batches/{batch_id}/approve",
            "POST",
            token=token,
        )
        approved = step.check(
            "approve batch",
            status == 200 and json.loads(body).get("status") == "approved",
            f"(status={status})",
        )

        if approved:
            status, body = call(
                args.base_url,
                f"/api/change-batches/{batch_id}/apply",
                "POST",
                {"confirmation": "CONFIRM ALL"},
                token=token,
            )
            result = json.loads(body) if status == 200 else {}
            terminal = result.get("status") in {
                "success",
                "partial_success",
                "failed",
            }
            step.check(
                "apply batch with exact CONFIRM ALL",
                status == 200 and terminal,
                f"(status={status}, batch={result.get('status')})",
            )
            for child in result.get("changes") or []:
                hostname = child.get("device", {}).get("hostname", "<unknown>")
                step.check(
                    f"review child result for {hostname}",
                    child.get("status") == "success",
                    child.get("error_message") or f"({child.get('status')})",
                )
            if result.get("status") == "partial_success":
                print(
                    "\nMANUAL FOLLOW-UP REQUIRED: at least one device failed; "
                    "review every failed child before retrying."
                )
            elif result.get("status") == "failed":
                print(
                    "\nMANUAL FOLLOW-UP REQUIRED: the batch failed; review every "
                    "child error before retrying."
                )
    else:
        step.skip("approve/apply batch", "preview inspection failed")

    print()
    if step.failures:
        print(f"FAILED: {', '.join(step.failures)}")
        return 1
    print("ALL STEPS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
