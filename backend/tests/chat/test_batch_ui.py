import json
import subprocess
from pathlib import Path


HARNESS = Path(__file__).with_name("batch_ui_harness.cjs")


def run_behavior_case(case_name: str):
    return subprocess.run(
        ["node", str(HARNESS), case_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def assert_behavior_case(case_name: str):
    result = run_behavior_case(case_name)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"case": case_name, "ok": True}


def test_index_contains_live_batch_card_contract(client):
    html = client.get("/").get_data(as_text=True)
    assert "message.payload.batch" in html
    assert "batchesById" in html
    assert "batch.confirmation_text" in html
    assert "approveBatch(batch.id)" in html
    assert "applyBatch(batch.id)" in html


def test_app_js_fetches_standalone_changes_and_batches():
    source = Path("src/network_copilot/static/js/app.js").read_text(encoding="utf-8")
    assert "/api/changes?standalone_only=true&limit=500" in source
    assert "/api/change-batches?limit=500" in source
    assert "body.confirmation" in source


def test_persisted_chat_snapshot_does_not_replace_live_batch_state():
    assert_behavior_case("stale_chat_snapshot")


def test_stale_batch_get_does_not_replace_newer_action_result():
    assert_behavior_case("stale_get_after_action")


def test_only_latest_concurrent_batch_refresh_can_update_state():
    assert_behavior_case("latest_refresh_wins")


def test_batch_refresh_does_not_launch_while_action_is_running():
    assert_behavior_case("refresh_skips_during_action")


def test_batch_actions_lock_per_batch_without_blocking_other_batches():
    assert_behavior_case("actions_lock_per_batch")


def test_batch_confirmation_is_exact_and_submitted_untrimmed():
    assert_behavior_case("confirmation_exact")


def test_logout_invalidates_batch_action_and_cleans_polling_timers():
    assert_behavior_case("logout_cleanup")
