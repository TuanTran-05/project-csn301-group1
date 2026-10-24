"""Concurrency, secret boundaries and malformed IDs; test database is RAM only."""
from copy import deepcopy
import json

import pytest
from cryptography.fernet import Fernet

from network_copilot.changes.model import ChangeBatch, ChangeRequest
from network_copilot.history_extension import HistoryManager
from network_copilot.history_extension.pending_service import workspace
from network_copilot.history_extension.protection import REDACTED

from .test_history_boundaries import prohibit_device_operations  # noqa: F401
from .test_pending_history import BASE, make_batch, move, preview, state


def opened(client, headers, kind, record_id):
    url = BASE + f"/{'changes' if kind == 'change' else 'batches'}/{record_id}"
    response = client.get(url + "/editor", headers=headers)
    assert response.status_code == 200, response.get_json()
    return url, response.get_json()


def journal(work):
    return deepcopy(work.history._histories), deepcopy(work.expected), work.revision_epoch


@pytest.mark.parametrize("kind", ["change", "batch"])
def test_two_editors_reject_stale_save_without_any_write(client, admin_headers, admin_user,
                                                       access_switch, dist_switch, db, kind):
    record = preview(client, admin_headers, access_switch) if kind == "change" else make_batch(admin_user, access_switch, dist_switch)
    record_id = record["id"] if kind == "change" else record.id
    url, first = opened(client, admin_headers, kind, record_id)
    _, second = opened(client, admin_headers, kind, record_id)
    assert first == second
    payload = {"description": "new"} if kind == "change" else {
        "description": "new", "changes": [{"id": c["id"], "description": "new"} for c in first["draft"]["changes"]]}
    assert client.patch(url, headers=admin_headers, json={**payload, "revision": first["revision"]}).status_code == 200
    work = workspace(admin_user.id)
    before = journal(work)
    payload["description"] = "stale editor"
    response = client.patch(url, headers=admin_headers, json={**payload, "revision": second["revision"]})
    assert response.status_code == 409
    assert response.get_json()["details"] == {"reason": "stale_revision", "reload_required": True}
    assert journal(work) == before
    model = ChangeRequest if kind == "change" else ChangeBatch
    assert db.session.get(model, record_id).description == "new"
    if kind == "batch":
        assert all(child.description == "new" for child in db.session.get(model, record_id).changes)
    move(client, admin_headers, "undo")
    assert db.session.get(model, record_id).description == "first"
    move(client, admin_headers, "redo")
    assert db.session.get(model, record_id).description == "new"


@pytest.mark.parametrize("action", ["undo_redo", "restore", "cancel", "approve", "external_edit"])
def test_open_editor_is_invalidated_by_lifecycle(client, admin_headers, admin_user, access_switch, db, action):
    created = preview(client, admin_headers, access_switch)
    url, editor = opened(client, admin_headers, "change", created["id"])
    if action == "undo_redo":
        move(client, admin_headers, "undo")
        move(client, admin_headers, "redo")  # Same content, different revision (ABA).
    elif action == "restore":
        backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
        assert client.post(BASE + "/restore", headers=admin_headers, json=backup).status_code == 200
    elif action in {"cancel", "approve"}:
        assert client.post(f"/api/changes/{created['id']}/{action}", headers=admin_headers).status_code == 200
    else:
        row = db.session.get(ChangeRequest, created["id"])
        row.description = "external update"
        db.session.commit()
    work = workspace(admin_user.id)
    before = journal(work)
    row_before = db.session.get(ChangeRequest, created["id"]).to_dict()
    assert client.patch(url, headers=admin_headers, json={"revision": editor["revision"], "description": "stale"}).status_code == 409
    assert journal(work) == before
    assert db.session.get(ChangeRequest, created["id"]).to_dict() == row_before


@pytest.mark.parametrize("bad_id", [[], {}, None, "", "1", True, 0, -1, 1.0])
def test_malformed_child_ids_are_structured_validation_without_mutation(client, admin_headers, admin_user,
                                                                     access_switch, dist_switch, db, bad_id):
    batch = make_batch(admin_user, access_switch, dist_switch)
    url, editor = opened(client, admin_headers, "batch", batch.id)
    body = {"revision": editor["revision"], "description": "changed", "changes": [
        {"id": child["id"], "description": "changed"} for child in editor["draft"]["changes"]]}
    body["changes"][0]["id"] = bad_id
    work = workspace(admin_user.id)
    before = journal(work)
    response = client.patch(url, headers=admin_headers, json=body)
    assert response.status_code == 422, response.get_json()
    assert response.get_json()["details"] == {"changes.0.id": ["Expected a positive integer"]}
    assert journal(work) == before
    assert db.session.get(ChangeBatch, batch.id).description == "first"
    assert all(child.description == "first" for child in batch.changes)


@pytest.mark.parametrize("revision", [None, "", [], {}, "invalid", "0" * 63])
def test_missing_or_malformed_revision_is_nonmutating(client, admin_headers, admin_user, access_switch, revision):
    created = preview(client, admin_headers, access_switch)
    work = workspace(admin_user.id)
    before = journal(work)
    response = client.patch(BASE + f"/changes/{created['id']}", headers=admin_headers,
                            json={"revision": revision, "description": "invalid"})
    assert response.status_code == 422
    assert "revision" in response.get_json()["details"]
    assert journal(work) == before


def test_invalid_edit_after_external_update_does_not_checkpoint(client, admin_headers, admin_user, access_switch, db):
    created = preview(client, admin_headers, access_switch)
    row = db.session.get(ChangeRequest, created["id"])
    row.description = "external update"
    db.session.commit()
    url, editor = opened(client, admin_headers, "change", created["id"])
    work = workspace(admin_user.id)
    before = journal(work)
    assert client.patch(url, headers=admin_headers, json={"revision": editor["revision"], "commands": []}).status_code == 422
    assert journal(work) == before
    assert row.description == "external update"


def test_edit_after_undo_starts_new_branch(client, admin_headers, access_switch):
    created = preview(client, admin_headers, access_switch)
    url, first = opened(client, admin_headers, "change", created["id"])
    assert client.patch(url, headers=admin_headers, json={"revision": first["revision"], "description": "second"}).status_code == 200
    move(client, admin_headers, "undo")
    assert state(client, admin_headers)["can_redo"]
    _, fresh = opened(client, admin_headers, "change", created["id"])
    assert fresh["revision"] != first["revision"]
    assert client.patch(url, headers=admin_headers, json={"revision": fresh["revision"], "description": "branch"}).status_code == 200
    assert not state(client, admin_headers)["can_redo"]
    assert move(client, admin_headers, "undo")["snapshot"]["changes"][0]["description"] == "first"
    assert move(client, admin_headers, "redo")["snapshot"]["changes"][0]["description"] == "branch"


def test_batch_edit_cas_failure_is_atomic(client, admin_headers, admin_user, access_switch, dist_switch, db, monkeypatch):
    from network_copilot.history_extension.pending_service import PendingWorkspace
    from network_copilot.errors import ConflictError
    batch = make_batch(admin_user, access_switch, dist_switch)
    url, editor = opened(client, admin_headers, "batch", batch.id)
    work = workspace(admin_user.id)
    before = journal(work)
    original = PendingWorkspace._cas
    calls = []
    def fail_second(self, row, values):
        calls.append(row.id)
        if len(calls) == 2:
            raise ConflictError("Concurrent update")
        return original(self, row, values)
    monkeypatch.setattr(PendingWorkspace, "_cas", fail_second)
    response = client.patch(url, headers=admin_headers, json={"revision": editor["revision"], "description": "changed",
                            "changes": [{"id": c["id"], "description": "changed"} for c in editor["draft"]["changes"]]})
    assert response.status_code == 409
    assert journal(work) == before
    assert db.session.get(ChangeBatch, batch.id).description == "first"
    assert all(c.description == "first" for c in batch.changes)


@pytest.mark.parametrize("path", ["/changes/1/editor", "/batches/1/editor", "/audit", "/backup"])
def test_editor_and_protected_exports_require_jwt_admin(client, admin_headers, viewer_headers, admin_user, db, path):
    assert client.get(BASE + path).status_code == 401
    assert client.get(BASE + path, headers={"Authorization": "Bearer invalid.token"}).status_code == 401
    assert client.get(BASE + path, headers=viewer_headers).status_code == 403
    # The role is read live, not from the token. (The sole Administrator can
    # never be deactivated, so demote the account behind the token's back.)
    admin_user.role = "TEACHER"
    db.session.commit()
    assert client.get(BASE + path, headers=admin_headers).status_code == 403


def test_edit_commit_failure_rolls_back_database_history_and_safe_error(client, admin_headers, admin_user,
                                                                     access_switch, db, monkeypatch, caplog):
    created = preview(client, admin_headers, access_switch)
    url, editor = opened(client, admin_headers, "change", created["id"])
    work = workspace(admin_user.id)
    before = journal(work)
    fake = "CommitExceptionFakeCredential123"
    def fail():
        raise RuntimeError(fake)
    monkeypatch.setattr(db.session, "commit", fail)
    response = client.patch(url, headers=admin_headers, json={"revision": editor["revision"], "description": "changed"})
    assert response.status_code == 500
    assert fake not in response.get_data(as_text=True) + caplog.text
    assert journal(work) == before
    assert db.session.get(ChangeRequest, created["id"]).description == "first"




def test_encrypted_backup_keeps_restore_exact_while_audit_and_diff_hide_secrets():
    fake = "FakeHistoryCredential_NoRealDevice123"
    manager = HistoryManager()
    first = {"commands": ["unknown-vendor " + fake], "password": fake, "description": fake}
    second = {"commands": ["enable secret 0 " + fake], "password": fake + "next", "description": fake}
    manager.push_state(first, "operator", "BASELINE", "baseline")
    manager.push_state(second, "operator", "EDIT", "enable secret 0 " + fake)
    backup = manager.export_backup_file()
    assert fake not in backup
    assert fake not in json.dumps(manager.export_audit_log())
    assert fake not in json.dumps(manager.get_diff())
    assert fake not in json.dumps(manager._history("default")["audit"])
    assert json.loads(backup)["audit_trail"][0]["snapshot"]["kind"] == "json-object"
    assert REDACTED in json.dumps(manager.get_diff())
    restored = HistoryManager()
    assert restored.restore_from_backup(backup) == second
    assert restored.undo() == first
    assert restored.redo() == second


def test_backup_key_and_authentication_failures_never_change_history():
    key = Fernet.generate_key()
    source = HistoryManager(backup_key=key)
    source.push_state({"password": "fake-password"}, "operator", "BASELINE", "baseline")
    backup = json.loads(source.export_backup_file())
    target = HistoryManager(backup_key=Fernet.generate_key())
    with pytest.raises(ValueError):
        target.restore_from_backup(backup)
    assert target.current_snapshot() is None
    matching = HistoryManager(backup_key=key)
    assert matching.restore_from_backup(backup) == {"password": "fake-password"}
    before = matching.status()
    altered = deepcopy(backup)
    altered["audit_trail"][0]["actor"] = "tampered"
    with pytest.raises(ValueError):
        matching.restore_from_backup(altered)
    altered = deepcopy(backup)
    altered["encrypted_history"] = altered["encrypted_history"][:-5] + "XXXXX"
    with pytest.raises(ValueError):
        matching.restore_from_backup(altered)
    assert matching.status() == before


def test_audit_only_redacted_and_plaintext_secret_backups_cannot_restore():
    manager = HistoryManager()
    manager.push_state({"commands": ["description initial"]}, "operator", "BASELINE", "baseline")
    before = manager.status()
    with pytest.raises(ValueError):
        manager.restore_from_backup(manager.export_audit_log())
    from network_copilot.history_extension.protection import decode_backup
    legacy = decode_backup(json.loads(manager.export_backup_file()), manager._cipher)
    for commands in ([REDACTED], ["enable secret 0 FakePlaintextPassword"]):
        data = deepcopy(legacy)
        data["audit_trail"][0]["snapshot"]["commands"] = commands
        with pytest.raises(ValueError) as exc:
            manager.restore_from_backup(data)
        assert "FakePlaintextPassword" not in str(exc.value)
        assert manager.status() == before


def test_pending_backup_and_audit_protected_restore_exact(client, admin_headers, admin_user, access_switch, db):
    fake = "FakePendingSecret_NotAnActualCredential123"
    created = preview(client, admin_headers, access_switch)
    url, editor = opened(client, admin_headers, "change", created["id"])
    commands = ["interface Gi0/2", "description " + fake]
    assert client.patch(url, headers=admin_headers, json={"revision": editor["revision"], "commands": commands,
                                                        "description": fake}).status_code == 200
    canonical = client.get(f"/api/changes/{created['id']}", headers=admin_headers).get_json()["commands"]
    response = client.get(BASE + "/backup", headers=admin_headers)
    assert fake not in response.get_data(as_text=True)
    backup = response.get_json()
    audit = client.get(BASE + "/audit", headers=admin_headers)
    assert audit.status_code == 200 and audit.get_json()["restore_supported"] is False
    assert fake not in audit.get_data(as_text=True)
    move(client, admin_headers, "undo")
    assert client.post(BASE + "/restore", headers=admin_headers, json=backup).status_code == 200
    row = db.session.get(ChangeRequest, created["id"])
    assert row.commands == canonical and row.description == fake
    assert "description " + fake in row.commands
    before = journal(workspace(admin_user.id))
    assert client.post(BASE + "/restore", headers=admin_headers, json=audit.get_json()).status_code == 400
    assert journal(workspace(admin_user.id)) == before




def test_invalid_backup_parser_does_not_echo_secret():
    fake = "FakeTimestampPassword_DoNotUse123"
    manager = HistoryManager()
    with pytest.raises(ValueError) as exc:
        manager.restore_from_backup({"backup_version": "1.0", "exported_at": fake, "audit_trail": []})
    assert fake not in str(exc.value)
