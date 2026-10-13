"""Pending DB/history integration; all data stays in test DB RAM."""
from copy import deepcopy
import json

import pytest
from concurrent.futures import ThreadPoolExecutor

from network_copilot.changes import batch_service
from network_copilot.changes.batch_service import BatchOperation
from network_copilot.changes.model import ChangeRequest, ChangeBatch
from network_copilot.history_extension.pending_service import workspace
from network_copilot.errors import ConflictError

from .test_history_boundaries import prohibit_device_operations  # noqa: F401

BASE = "/api/pending-history"


def preview(client, headers, device, description="first"):
    response = client.post("/api/changes/preview", headers=headers, json={
        "device_id": device.id, "commands": ["interface Gi0/2", "description " + description],
        "description": description,
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def edit(client, url, *, headers, json):
    """Existing edit scenarios now use the required editor-open contract."""
    opened = client.get(url + "/editor", headers=headers)
    data = deepcopy(json)
    if opened.status_code == 200:
        data["revision"] = opened.get_json()["revision"]
    return client.patch(url, headers=headers, json=data)


def legacy_backup(client, headers, admin_user):
    """Build a nonsensitive 1.0 fixture to test semantic import validation."""
    from network_copilot.history_extension.protection import decode_backup
    envelope = client.get(BASE + "/backup", headers=headers).get_json()
    return decode_backup(envelope, workspace(admin_user.id).history._cipher)


def state(client, headers):
    response = client.get(BASE, headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def move(client, headers, action):
    response = client.post(BASE + "/" + action, headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def pending_ids(client, headers):
    response = client.get("/api/changes?standalone_only=true", headers=headers)
    return [item["id"] for item in response.get_json()["items"] if item["status"] == "pending_approval"]


def test_creation_is_automatically_captured_and_updates_database(client, admin_headers, access_switch, db):
    assert not state(client, admin_headers)["can_undo"]
    created = preview(client, admin_headers, access_switch)
    assert state(client, admin_headers)["can_undo"]
    assert pending_ids(client, admin_headers) == [created["id"]]
    assert move(client, admin_headers, "undo")["snapshot"]["changes"] == []
    assert pending_ids(client, admin_headers) == []
    assert db.session.get(ChangeRequest, created["id"]).status == "cancelled"
    assert move(client, admin_headers, "redo")["snapshot"]["changes"][0]["id"] == created["id"]
    assert pending_ids(client, admin_headers) == [created["id"]]
    assert db.session.query(ChangeRequest).count() == 1


def test_edit_undo_redo_and_multiple_states(client, admin_headers, access_switch):
    created = preview(client, admin_headers, access_switch)
    for text in ("second", "third"):
        response = edit(client, BASE + f"/changes/{created['id']}", headers=admin_headers, json={
            "commands": ["interface Gi0/2", "description " + text], "description": text})
        assert response.status_code == 200, response.get_json()
    assert move(client, admin_headers, "undo")["snapshot"]["changes"][0]["description"] == "second"
    move(client, admin_headers, "undo")
    actual = client.get(f"/api/changes/{created['id']}", headers=admin_headers).get_json()
    assert actual["description"] == "first" and "description first" in actual["commands"]
    assert move(client, admin_headers, "redo")["snapshot"]["changes"][0]["description"] == "second"
    assert move(client, admin_headers, "redo")["snapshot"]["changes"][0]["description"] == "third"


def test_undo_then_new_creation_discards_redo_preserves_audit(client, admin_headers, access_switch):
    first = preview(client, admin_headers, access_switch, "first")
    second = preview(client, admin_headers, access_switch, "second")
    move(client, admin_headers, "undo")
    third = preview(client, admin_headers, access_switch, "third")
    assert set(pending_ids(client, admin_headers)) == {first["id"], third["id"]}
    assert not state(client, admin_headers)["can_redo"]
    assert client.post(BASE + "/redo", headers=admin_headers).status_code == 409
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    from network_copilot.history_extension.protection import cipher_for, decode_backup
    private = decode_backup(backup, cipher_for())
    assert any(second["id"] in [s["id"] for s in e["snapshot"]["changes"]] for e in private["audit_trail"])


def test_cancel_unapproved_is_reversible(client, admin_headers, access_switch):
    created = preview(client, admin_headers, access_switch)
    assert client.post(f"/api/changes/{created['id']}/cancel", headers=admin_headers).status_code == 200
    assert pending_ids(client, admin_headers) == []
    move(client, admin_headers, "undo")
    assert pending_ids(client, admin_headers) == [created["id"]]
    move(client, admin_headers, "redo")
    assert pending_ids(client, admin_headers) == []


def make_batch(admin_user, *devices):
    return batch_service.create_batch_preview(admin_user.id, [BatchOperation(
        [d.hostname for d in devices], "config", ["interface Gi0/2", "description first"], [])], "first")


def test_batch_create_edit_cancel_are_atomic_and_reversible(client, admin_headers, admin_user, access_switch, dist_switch, db):
    batch = make_batch(admin_user, access_switch, dist_switch)
    bid, ids = batch.id, [c.id for c in batch.changes]
    move(client, admin_headers, "undo")
    assert db.session.get(ChangeBatch, bid).status == "cancelled"
    assert all(db.session.get(ChangeRequest, i).status == "cancelled" for i in ids)
    move(client, admin_headers, "redo")
    response = edit(client, BASE + f"/batches/{bid}", headers=admin_headers, json={
        "description": "edited", "changes": [{"id": i, "description": "edited", "commands": ["interface Gi0/2", "description edited"]} for i in ids]})
    assert response.status_code == 200, response.get_json()
    move(client, admin_headers, "undo")
    assert db.session.get(ChangeBatch, bid).description == "first"
    assert all(db.session.get(ChangeRequest, i).description == "first" for i in ids)
    move(client, admin_headers, "redo")
    assert all(db.session.get(ChangeRequest, i).description == "edited" for i in ids)
    assert client.post(f"/api/change-batches/{bid}/cancel", headers=admin_headers).status_code == 200
    move(client, admin_headers, "undo")
    assert all(db.session.get(ChangeRequest, i).status == "pending_approval" for i in ids)


def test_backup_restore_writes_pending_and_retains_redo(client, admin_headers, access_switch):
    first = preview(client, admin_headers, access_switch)
    second = preview(client, admin_headers, access_switch, "second")
    move(client, admin_headers, "undo")
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    move(client, admin_headers, "redo")
    response = client.post(BASE + "/restore", headers=admin_headers, json=backup)
    assert response.status_code == 200, response.get_json()
    assert pending_ids(client, admin_headers) == [first["id"]]
    assert response.get_json()["can_redo"]
    move(client, admin_headers, "redo")
    assert set(pending_ids(client, admin_headers)) == {first["id"], second["id"]}


@pytest.mark.parametrize("terminal", ["approved", "running", "success", "failed"])
def test_external_state_invalidates_history_without_resurrection(client, admin_headers, access_switch, db, terminal):
    created = preview(client, admin_headers, access_switch)
    row = db.session.get(ChangeRequest, created["id"])
    row.status = terminal
    db.session.commit()
    assert client.post(BASE + "/undo", headers=admin_headers).status_code == 409
    assert db.session.get(ChangeRequest, row.id).status == terminal
    assert not state(client, admin_headers)["can_undo"]


def test_approved_then_cancelled_never_reopens(client, admin_headers, access_switch):
    created = preview(client, admin_headers, access_switch)
    assert client.post(f"/api/changes/{created['id']}/approve", headers=admin_headers).status_code == 200
    assert client.post(f"/api/changes/{created['id']}/cancel", headers=admin_headers).status_code == 200
    assert client.post(BASE + "/undo", headers=admin_headers).status_code == 409
    assert client.get(f"/api/changes/{created['id']}", headers=admin_headers).get_json()["status"] == "cancelled"


def test_empty_invalid_inputs_and_batch_child_edit_are_safe(client, admin_headers, admin_user, access_switch, dist_switch):
    for action in ("undo", "redo"):
        assert client.post(BASE + "/" + action, headers=admin_headers).status_code == 409
    batch = make_batch(admin_user, access_switch, dist_switch)
    before = state(client, admin_headers)
    assert edit(client, BASE + f"/changes/{batch.changes[0].id}", headers=admin_headers, json={"description": "bad"}).status_code == 422
    assert edit(client, BASE + f"/batches/{batch.id}", headers=admin_headers, json={"description": "bad", "changes": []}).status_code == 422
    assert client.post(BASE + "/restore", headers=admin_headers, json={}).status_code == 400
    assert state(client, admin_headers) == before


def test_invalid_single_edit_does_not_commit_or_move_history(client, admin_headers, access_switch):
    created = preview(client, admin_headers, access_switch)
    before = state(client, admin_headers)
    for data in ({"commands": []}, {"status": "approved"}, {"commands": "bad"}):
        assert edit(client, BASE + f"/changes/{created['id']}", headers=admin_headers, json=data).status_code == 422
        assert state(client, admin_headers) == before


def test_compare_and_swap_failure_rolls_back_whole_batch(client, admin_headers, admin_user, access_switch, dist_switch, monkeypatch):
    batch = make_batch(admin_user, access_switch, dist_switch)
    work = workspace(admin_user.id)
    original = work._cas
    calls = []
    def fail_second(row, values):
        calls.append(row.id)
        if len(calls) == 2:
            raise ConflictError("Concurrent child update")
        return original(row, values)
    before = state(client, admin_headers)
    monkeypatch.setattr(work, "_cas", fail_second)
    assert client.post(BASE + "/undo", headers=admin_headers).status_code == 409
    assert state(client, admin_headers) == before
    assert client.get(f"/api/change-batches/{batch.id}", headers=admin_headers).get_json()["status"] == "pending_approval"


def test_identity_change_and_partial_batch_backup_are_rejected(client, admin_headers, admin_user, access_switch, dist_switch, db):
    batch = make_batch(admin_user, access_switch, dist_switch)
    backup = legacy_backup(client, admin_headers, admin_user)
    current_index = backup["history_state"]["undo_stack"][-1]
    backup["audit_trail"][current_index]["snapshot"]["changes"].pop()
    assert client.post(BASE + "/restore", headers=admin_headers, json=backup).status_code == 422
    access_switch.device_type = "cisco_asa"
    db.session.commit()
    assert edit(client, BASE + f"/batches/{batch.id}", headers=admin_headers, json={
        "description": "changed", "changes": [{"id": c.id, "description": "changed"} for c in batch.changes]}).status_code == 409


def test_permissions_owner_and_default_topology(client, admin_headers, viewer_headers, access_switch, db):
    assert client.get(BASE).status_code == 401
    assert client.get(BASE, headers=viewer_headers).status_code == 403
    assert client.get(BASE + "?topology_id=second", headers=admin_headers).status_code == 422
    created = preview(client, admin_headers, access_switch)
    from network_copilot.auth.model import User
    from network_copilot.auth.service import issue_token
    other = User(username="other-pending", role="ADMIN")
    other.set_password("OtherPendingTestPassword!")
    db.session.add(other)
    db.session.commit()
    headers = {"Authorization": "Bearer " + issue_token(other)}
    assert state(client, headers)["snapshot"]["changes"] == []
    assert edit(client, BASE + f"/changes/{created['id']}", headers=headers, json={"description": "other"}).status_code == 409
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    assert client.post(BASE + "/restore", headers=headers, json=backup).status_code == 403


def test_backup_after_approval_barrier_restores_current_scope(client, admin_headers, access_switch):
    approved = preview(client, admin_headers, access_switch)
    assert client.post(f"/api/changes/{approved['id']}/approve", headers=admin_headers).status_code == 200
    state(client, admin_headers)
    editable = preview(client, admin_headers, access_switch, "editable")
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    move(client, admin_headers, "undo")
    response = client.post(BASE + "/restore", headers=admin_headers, json=backup)
    assert response.status_code == 200, response.get_json()
    assert pending_ids(client, admin_headers) == [editable["id"]]
    assert client.get(f"/api/changes/{approved['id']}", headers=admin_headers).get_json()["status"] == "approved"


def test_batch_invalid_child_and_tampered_backup_are_atomic(client, admin_headers, admin_user, access_switch, dist_switch):
    batch = make_batch(admin_user, access_switch, dist_switch)
    before = state(client, admin_headers)
    body = {"description": "changed", "changes": [{"id": c.id, "commands": ["interface Gi0/2", "description changed"]} for c in batch.changes]}
    body["changes"][-1]["commands"] = []
    assert edit(client, BASE + f"/batches/{batch.id}", headers=admin_headers, json=body).status_code == 422
    assert state(client, admin_headers) == before
    backup = legacy_backup(client, admin_headers, admin_user)
    idx = backup["history_state"]["undo_stack"][-1]
    backup["audit_trail"][idx]["snapshot"]["changes"][0]["target_device_type"] = "cisco_asa"
    assert client.post(BASE + "/restore", headers=admin_headers, json=backup).status_code == 409
    assert state(client, admin_headers) == before


def test_sensitive_new_verification_is_rejected_and_risk_is_recomputed(client, admin_headers, access_switch):
    created = preview(client, admin_headers, access_switch)
    before = state(client, admin_headers)
    endpoint = BASE + f"/changes/{created['id']}"
    assert edit(client, endpoint, headers=admin_headers, json={"verification_commands": ["show running-config"]}).status_code == 403
    assert state(client, admin_headers) == before
    response = edit(client, endpoint, headers=admin_headers, json={"commands": ["interface Gi0/2", "shutdown"]})
    assert response.status_code == 200, response.get_json()
    changed = client.get(f"/api/changes/{created['id']}", headers=admin_headers).get_json()
    assert changed["requires_confirmation"] and changed["risk_level"] == "high"
    move(client, admin_headers, "undo")
    changed = client.get(f"/api/changes/{created['id']}", headers=admin_headers).get_json()
    assert not changed["requires_confirmation"]


def test_failed_commit_keeps_pending_and_history(client, admin_headers, admin_user, access_switch, monkeypatch):
    created = preview(client, admin_headers, access_switch)
    before = state(client, admin_headers)
    from network_copilot.extensions import db
    original = db.session.commit
    def fail():
        raise RuntimeError("test commit failure")
    monkeypatch.setattr(db.session, "commit", fail)
    assert client.post(BASE + "/undo", headers=admin_headers).status_code == 500
    monkeypatch.setattr(db.session, "commit", original)
    assert state(client, admin_headers) == before
    assert pending_ids(client, admin_headers) == [created["id"]]


def test_history_writes_and_core_approval_hold_the_same_request_lock(app, client, admin_headers, admin_user, access_switch, monkeypatch):
    lock = app.extensions["pending_history"]["request_lock"]
    def available_from_other_thread():
        def probe():
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
            return acquired
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(probe).result(timeout=5)
    created = preview(client, admin_headers, access_switch)
    work = workspace(admin_user.id)
    original_cas = work._cas
    def checked_cas(row, values):
        assert not available_from_other_thread()
        return original_cas(row, values)
    monkeypatch.setattr(work, "_cas", checked_cas)
    move(client, admin_headers, "undo")
    move(client, admin_headers, "redo")
    from network_copilot.changes import service
    original_approve = service.approve
    def checked_approve(*args, **kwargs):
        assert not available_from_other_thread()
        return original_approve(*args, **kwargs)
    monkeypatch.setattr(service, "approve", checked_approve)
    assert client.post(f"/api/changes/{created['id']}/approve", headers=admin_headers).status_code == 200
    assert available_from_other_thread()
    assert client.post(BASE + "/undo").status_code == 401
    assert available_from_other_thread()
