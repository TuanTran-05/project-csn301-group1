"""Prove database -> unmodified app.js refresh/getters -> pending UI state."""
import json
from pathlib import Path
import subprocess

from .test_history_boundaries import prohibit_device_operations  # noqa: F401
from .test_pending_history import make_batch


def test_pending_history_roundtrip_with_real_core_refresh_and_api(client, admin_headers, admin_user, access_switch, dist_switch):
    batch = make_batch(admin_user, access_switch, dist_switch)
    harness = Path(__file__).resolve().parents[2] / "extension_tests" / "pending_api_ui_harness.cjs"
    with subprocess.Popen(["node", str(harness), str(access_switch.id), str(batch.id), str(admin_user.id)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8") as process:
        try:
            done = None
            for line in process.stdout:
                request = json.loads(line)
                if request.get("done"):
                    done = request
                    break
                url = request["url"]
                assert url.startswith(("/api/pending-history", "/api/changes", "/api/change-batches"))
                assert "/apply" not in url and "/approve" not in url
                options = request.get("options", {})
                response = client.open(url, method=options.get("method", "GET"), headers=admin_headers,
                                       data=options.get("body"), content_type="application/json")
                process.stdin.write(json.dumps({"id": request["id"], "status": response.status_code,
                                               "data": response.get_json()}) + "\n")
                process.stdin.flush()
            process.wait(timeout=35)
            stderr = process.stderr.read()
            assert process.returncode == 0, stderr
            assert done is not None, stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    actual = client.get("/api/changes?standalone_only=true&limit=500", headers=admin_headers).get_json()
    assert set(done["pendingIds"]) == {row["id"] for row in actual["items"] if row["status"] == "pending_approval"}
    actual_batch = client.get(f"/api/change-batches/{batch.id}", headers=admin_headers).get_json()
    assert actual_batch["status"] == "pending_approval"
    assert all(c["status"] == "pending_approval" for c in actual_batch["changes"])
