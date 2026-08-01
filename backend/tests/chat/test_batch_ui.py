from pathlib import Path


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
