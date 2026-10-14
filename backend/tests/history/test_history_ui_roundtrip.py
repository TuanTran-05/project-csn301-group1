"""Real Flask API/HistoryManager connected to the JS controller via local pipes.

DOM and Alpine registration are minimal test doubles; no browser is claimed.
"""
import json
from pathlib import Path
import subprocess

from network_copilot.changes.model import ChangeRequest, ChangeBatch
from network_copilot.devices.model import Device

from .test_history_boundaries import prohibit_device_operations  # noqa: F401


def test_ui_clicks_and_draft_callbacks_roundtrip_through_real_api(client, admin_headers, db):
    harness = Path(__file__).resolve().parents[2] / "extension_tests" / "history_api_ui_harness.cjs"
    transcript = []
    with subprocess.Popen(["node", str(harness)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8") as process:
        try:
            done = None
            for line in process.stdout:
                request = json.loads(line)
                if request.get("done"):
                    done = request
                    break
                assert request["url"].startswith("/api/draft-history")
                options = request.get("options", {})
                response = client.open(request["url"], method=options.get("method", "GET"),
                                       headers=admin_headers, data=options.get("body"),
                                       content_type="application/json")
                transcript.append((request["url"], response.status_code))
                process.stdin.write(json.dumps({"id": request["id"], "status": response.status_code,
                                               "data": response.get_json()}) + "\n")
                process.stdin.flush()
            process.wait(timeout=20)
            stderr = process.stderr.read()
            assert process.returncode == 0, stderr
            assert done and done["requests"] == len(transcript), stderr
            assert any("/redo" in url for url, status in transcript)
            assert any("/restore" in url and status == 200 for url, status in transcript)
            assert sum(status == 400 for url, status in transcript) == 1
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert db.session.query(ChangeRequest).count() == 0
    assert db.session.query(ChangeBatch).count() == 0
    assert db.session.query(Device).count() == 0
