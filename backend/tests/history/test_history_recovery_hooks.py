"""Recovery, core hook and key boundaries. Suite SQLite RAM only; no devices.

Run with the offline guard runner before collection, not a live application.
Failures are retained as evidence; these tests never repair the implementation.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import subprocess
import sys
from threading import Event

from cryptography.fernet import Fernet
from flask import request
import pytest

from network_copilot.audit.model import AuditLog
from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.changes.model import ChangeBatch, ChangeRequest
from network_copilot.errors import ConflictError, ValidationError
from network_copilot.history_extension import HistoryManager
from network_copilot.history_extension import pending_service
from network_copilot.history_extension.pending_service import workspace
from network_copilot.history_extension.protection import BackupSizeError, decode_backup

from .test_history_boundaries import prohibit_device_operations  # noqa: F401
from .test_history_final_regressions import create_draft, legacy_11, patch_for
from .test_history_hardening import journal, opened
from .test_pending_history import BASE, make_batch, move, preview


@pytest.fixture(autouse=True)
def confirm_ram_only(app, db):
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    assert db.engine.url.database == ":memory:"
    assert not app.config["MONITORING_ENABLED"]


def work_state(work):
    return journal(work), work.recovery_required


def database_state(db):
    """Copy every pending model column, without printing configuration on failure."""
    db.session.expire_all()
    return {model.__tablename__: [
        {column.key: deepcopy(getattr(row, column.key)) for column in model.__table__.columns}
        for row in db.session.query(model).order_by(model.id).all()
    ] for model in (ChangeRequest, ChangeBatch)}


def draft_with_redo(client, headers, user, access, dist, kind):
    record_id, children = create_draft(client, headers, user, access, dist, kind)
    url, editor = opened(client, headers, kind, record_id)
    assert client.patch(url, headers=headers, json=patch_for(editor, kind, "second")).status_code == 200
    move(client, headers, "undo")
    work = workspace(user.id)
    assert work.history.can_undo() and work.history.can_redo()
    return record_id, children, work


def assert_active_rows_tracked(work):
    snapshot, _ = work.read()
    active = {("change", row["id"]) for row in snapshot["changes"]}
    active |= {("batch", row["id"]) for row in snapshot["batches"]}
    assert active <= set(work.expected), "Active pending rows are missing from CAS tracking"
    assert snapshot == work.history.current_snapshot(), "RAM snapshot differs from pending DB"


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("operation", ["editor", "patch", "undo", "redo", "restore"])
def test_recovery_fences_every_pending_entry_point(client, admin_headers, admin_user,
                                                  access_switch, dist_switch, db, kind, operation):
    record_id, _, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    url, editor = opened(client, admin_headers, kind, record_id)
    backup = json.loads(work.history.export_backup_file())
    work.recovery_required = True
    before, db_before = work_state(work), database_state(db)
    if operation == "editor":
        response = client.get(url + "/editor", headers=admin_headers)
    elif operation == "patch":
        response = client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "blocked"))
    else:
        response = client.post(BASE + "/" + operation, headers=admin_headers,
                               **({"json": backup} if operation == "restore" else {}))
    assert response.status_code == 409
    assert response.get_json()["details"] == {"reason": "commit_outcome_unknown", "reload_required": True}
    assert work_state(work) == before
    assert database_state(db) == db_before


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("suffix", ["", "/backup", "/audit"])
def test_get_reconciles_before_clearing_recovery(client, admin_headers, admin_user,
                                                access_switch, dist_switch, db, monkeypatch, kind, suffix):
    record_id, children, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    db.session.get(ChangeRequest, children[0]).description = "external-current"
    db.session.commit()
    work.recovery_required = True
    before, db_before = journal(work), database_state(db)
    original, calls = work.synchronize, []

    def synchronize(actor):
        assert work.recovery_required
        result = original(actor)
        assert work.recovery_required  # status(), not synchronize(), clears it.
        calls.append(True)
        return result

    monkeypatch.setattr(work, "synchronize", synchronize)
    response = client.get(BASE + suffix, headers=admin_headers)
    assert response.status_code == 200 and calls == [True]
    assert not work.recovery_required
    assert journal(work) != before
    assert not work.history.can_undo() and not work.history.can_redo()
    assert_active_rows_tracked(work)
    assert database_state(db) == db_before
    if suffix == "/backup":
        private = decode_backup(response.get_json(), work.history._cipher)
        index = private["history_state"]["undo_stack"][-1]
        assert private["audit_trail"][index]["snapshot"] == work.read()[0]
    elif suffix == "/audit":
        assert response.get_json()["restore_supported"] is False
        assert response.get_json()["audit_trail"][-1]["action_type"] == "CHECKPOINT"
    else:
        assert response.get_json()["snapshot"] == work.read()[0]


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("suffix", ["", "/backup", "/audit"])
@pytest.mark.parametrize("failure", ["read", "synchronize"])
def test_failed_get_retains_recovery_and_history(client, admin_headers, admin_user,
                                                access_switch, dist_switch, db, monkeypatch, caplog,
                                                kind, suffix, failure):
    _, _, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    work.recovery_required = True
    before, db_before = work_state(work), database_state(db)
    marker = "FakeReadFault_DoNotExpose"
    original_read = work.read

    def fail(*args):
        assert work.recovery_required
        if failure == "synchronize":
            original_read()  # A successful read alone does not complete synchronize.
        raise RuntimeError(marker)

    with monkeypatch.context() as scoped:
        scoped.setattr(work, failure, fail)
        response = client.get(BASE + suffix, headers=admin_headers)
    assert response.status_code == 500
    assert marker not in caplog.text + response.get_data(as_text=True)
    assert work_state(work) == before
    assert database_state(db) == db_before
    assert client.get(BASE + suffix, headers=admin_headers).status_code == 200
    assert not work.recovery_required


@pytest.mark.parametrize("kind", ["change", "batch"])
def test_backup_export_failure_does_not_reinstate_reconciled_flag(client, admin_headers, admin_user,
                                                                 access_switch, dist_switch, db,
                                                                 monkeypatch, kind):
    _, children, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    db.session.get(ChangeRequest, children[0]).description = "external-current"
    db.session.commit()
    work.recovery_required = True
    db_before = database_state(db)

    def fail_export(*args):
        assert not work.recovery_required
        assert_active_rows_tracked(work)
        raise BackupSizeError("Injected export limit")

    monkeypatch.setattr(work.history, "export_backup_file", fail_export)
    response = client.get(BASE + "/backup", headers=admin_headers)
    assert response.status_code == 422 and response.get_json()["error"] == "backup_too_large"
    assert not work.recovery_required
    assert not work.history.can_undo() and not work.history.can_redo()
    assert database_state(db) == db_before


def core_operation(client, headers, user, access, dist, kind, operation, record_id):
    if operation == "create":
        if kind == "change":
            return preview(client, headers, access, "new-core-draft")["id"]
        return make_batch(user, access, dist).id
    prefix = "/api/changes/" if kind == "change" else "/api/change-batches/"
    assert client.post(prefix + str(record_id) + "/cancel", headers=headers).status_code == 200
    return record_id


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("operation", ["create", "cancel"])
@pytest.mark.parametrize("fault", ["success", "capture_read", "record_read"])
def test_core_hooks_remain_fail_soft_while_recovery_is_set(client, admin_headers, admin_user,
                                                          access_switch, dist_switch, db,
                                                          monkeypatch, caplog, kind, operation, fault):
    record_id, _, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    snapshot_before = work.read()[0]
    work.recovery_required = True
    before = work_state(work)
    marker = "FakeCoreHookFault_DoNotExpose"
    read_calls, original_read = [], work.read

    def read():
        read_calls.append(True)
        if len(read_calls) == (1 if fault == "capture_read" else 2):
            raise RuntimeError(marker)
        return original_read()

    with monkeypatch.context() as scoped:
        if fault != "success":
            scoped.setattr(work, "read", read)
        affected = core_operation(client, admin_headers, admin_user, access_switch, dist_switch,
                                  kind, operation, record_id)
    model = ChangeRequest if kind == "change" else ChangeBatch
    db.session.expire_all()
    row = db.session.get(model, affected)
    assert row.requested_by_id == admin_user.id
    assert row.status == ("pending_approval" if operation == "create" else "cancelled")
    if kind == "batch":
        assert all(child.status == row.status for child in row.changes)
    assert work.recovery_required  # Core hooks do not call status() to clear it.
    assert marker not in caplog.text
    snapshot_after = work.read()[0]
    assert snapshot_after != snapshot_before
    if fault == "success":
        assert journal(work) != before[0]
        private = decode_backup(json.loads(work.history.export_backup_file()), work.history._cipher)
        event = private["audit_trail"][-1]
        expected_action = ("CREATE_PENDING" if operation == "create" else "CANCEL_PENDING")
        assert event["action_type"] == expected_action + ("_BATCH" if kind == "batch" else "")
        assert event["actor"] == admin_user.username
        assert not work.history.can_redo()
    else:
        assert work_state(work) == before
        expected_log = "Cannot capture" if fault == "capture_read" else "Cannot record"
        assert expected_log in caplog.text
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert not work.recovery_required
    assert_active_rows_tracked(work)
    if fault == "success":
        move(client, admin_headers, "undo")
        assert work.read()[0] == snapshot_before
        move(client, admin_headers, "redo")
        assert work.read()[0] == snapshot_after
    else:
        assert not work.history.can_undo() and not work.history.can_redo()
        assert work.history.export_audit_log()["audit_trail"][-1]["action_type"] == "CHECKPOINT"


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("operation", ["create", "cancel"])
def test_core_record_fault_after_ram_push_recovers_complete_tracking(client, admin_headers, admin_user,
                                                                   access_switch, dist_switch, db,
                                                                   monkeypatch, caplog, kind, operation):
    record_id, _, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    work.recovery_required = True
    before = work_state(work)
    original_push = work.history.push_state
    fired = []

    def push_then_fail(*args, **kwargs):
        result = original_push(*args, **kwargs)
        fired.append(True)
        raise RuntimeError("FakePostPushFault_DoNotExpose")

    with monkeypatch.context() as scoped:
        scoped.setattr(work.history, "push_state", push_then_fail)
        affected = core_operation(client, admin_headers, admin_user, access_switch, dist_switch,
                                  kind, operation, record_id)
    model = ChangeRequest if kind == "change" else ChangeBatch
    db.session.expire_all()
    assert fired == [True]
    assert db.session.get(model, affected).status == ("pending_approval" if operation == "create" else "cancelled")
    assert "FakePostPushFault_DoNotExpose" not in caplog.text
    assert work.recovery_required and journal(work) != before[0]
    # A partially changed RAM journal is allowed here. After explicit reconcile,
    # every active DB row must be tracked before Undo/Redo can safely proceed.
    db_after = database_state(db)
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert not work.recovery_required
    assert_active_rows_tracked(work)
    assert database_state(db) == db_after


@pytest.mark.parametrize("kind", ["change", "batch"])
def test_other_admin_cancel_keeps_actor_workspace_separate_from_owner(client, admin_headers, admin_user,
                                                                    access_switch, dist_switch, db, kind):
    record_id, children, owner_work = draft_with_redo(client, admin_headers, admin_user,
                                                     access_switch, dist_switch, kind)
    # Two administrator identities (as before and after an admin transfer):
    # lift the single-admin index in this test database only.
    from sqlalchemy import text
    db.session.execute(text("DROP INDEX uq_single_admin"))
    actor = User(username="cancel-actor", role="ADMIN")
    actor.set_password("FakeActorPassword_ForRAMTests123!")
    db.session.add(actor)
    db.session.commit()
    actor_headers = {"Authorization": "Bearer " + issue_token(actor)}
    _, _, actor_work = draft_with_redo(client, actor_headers, actor, access_switch, dist_switch, "change")
    owner_work.recovery_required = actor_work.recovery_required = True
    owner_before, actor_before = work_state(owner_work), work_state(actor_work)
    core_operation(client, actor_headers, actor, access_switch, dist_switch, kind, "cancel", record_id)
    assert work_state(actor_work) == actor_before
    assert work_state(owner_work) == owner_before
    assert all(db.session.get(ChangeRequest, child).requested_by_id == admin_user.id for child in children)
    assert all(db.session.get(ChangeRequest, child).status == "cancelled" for child in children)
    audit_action = "change.cancel" if kind == "change" else "batch.cancel"
    event = db.session.query(AuditLog).filter_by(action=audit_action).order_by(AuditLog.id.desc()).first()
    assert event is not None and event.user_id == actor.id
    # Core cancellation records its actor in AuditLog. The owner's history is
    # invalidated on its next GET, rather than attributing actor work to owner.
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert not owner_work.recovery_required
    assert actor_work.recovery_required and work_state(actor_work) == actor_before
    assert not owner_work.history.can_undo() and not owner_work.history.can_redo()
    assert client.post(BASE + "/undo", headers=admin_headers).status_code == 409
    assert all(db.session.get(ChangeRequest, child).status == "cancelled" for child in children)


@pytest.mark.parametrize("kind, missing_kind", [("change", "change"), ("batch", "change"), ("batch", "batch")])
@pytest.mark.parametrize("suffix", ["", "/backup", "/audit"])
def test_missing_active_tracking_reconciles_once_and_invalidates_editor(client, admin_headers, admin_user,
                                                                       access_switch, dist_switch, db,
                                                                       kind, missing_kind, suffix):
    record_id, children, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    url, editor = opened(client, admin_headers, kind, record_id)
    missing_id = record_id if missing_kind == "batch" else children[0]
    work.expected.pop((missing_kind, missing_id))
    work.recovery_required = True
    before, db_before = journal(work), database_state(db)
    history, cipher, lock = work.history, work.history._cipher, work.history._lock
    assert work.history.current_snapshot() == work.read()[0]

    assert client.get(BASE + suffix, headers=admin_headers).status_code == 200
    assert not work.recovery_required
    assert work.history is history and history._cipher is cipher and history._lock is lock
    assert_active_rows_tracked(work)
    old, new = before[0]["default"], history._histories["default"]
    for field in ("audit", "events", "states"):
        assert new[field][:-1] == old[field]
        assert len(new[field]) == len(old[field]) + 1
    assert new["events"][-1]["action_type"] == "CHECKPOINT"
    assert new["undo"] == [len(new["audit"]) - 1] and new["redo"] == []
    assert work.revision_epoch == before[2] + 1
    assert database_state(db) == db_before

    reconciled = work_state(work)
    assert client.get(BASE + suffix, headers=admin_headers).status_code == 200
    assert work_state(work) == reconciled
    response = client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "stale-after-recovery"))
    assert response.status_code == 409
    assert response.get_json()["details"] == {"reason": "stale_revision", "reload_required": True}
    assert work_state(work) == reconciled
    assert database_state(db) == db_before


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("suffix", ["", "/backup", "/audit"])
@pytest.mark.parametrize("recovery", [False, True])
def test_normal_get_keeps_cancelled_tracking_and_redo(client, admin_headers, admin_user,
                                                     access_switch, dist_switch, db, kind, suffix, recovery):
    record_id, children = create_draft(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    move(client, admin_headers, "undo")
    work = workspace(admin_user.id)
    keys = {("change", child) for child in children}
    if kind == "batch":
        keys.add(("batch", record_id))
    assert work.history.current_snapshot() == {"schema": "pending-v1", "changes": [], "batches": []}
    assert keys <= set(work.expected) and work.history.can_redo()
    work.recovery_required = recovery
    before, db_before = journal(work), database_state(db)
    history, cipher, lock = work.history, work.history._cipher, work.history._lock

    for _ in range(2):
        assert client.get(BASE + suffix, headers=admin_headers).status_code == 200
        assert not work.recovery_required
        assert journal(work) == before
        assert database_state(db) == db_before
        assert work.history is history and history._cipher is cipher and history._lock is lock
    move(client, admin_headers, "redo")
    assert all(db.session.get(ChangeRequest, child).status == "pending_approval" for child in children)
    if kind == "batch":
        assert db.session.get(ChangeBatch, record_id).status == "pending_approval"
    assert_active_rows_tracked(work)
    move(client, admin_headers, "undo")
    assert all(db.session.get(ChangeRequest, child).status == "cancelled" for child in children)
    if kind == "batch":
        assert db.session.get(ChangeBatch, record_id).status == "cancelled"


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("suffix", ["", "/backup", "/audit"])
@pytest.mark.parametrize("failure", ["fingerprint", "fork", "checkpoint_before", "checkpoint_after"])
def test_failed_tracking_reconcile_does_not_publish_partial_ram(client, admin_headers, admin_user,
                                                               access_switch, dist_switch, db,
                                                               monkeypatch, caplog, kind, suffix, failure):
    _, children, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    work.expected.pop(("change", children[0]))
    work.recovery_required = True
    before, db_before = work_state(work), database_state(db)
    history, cipher, lock = work.history, work.history._cipher, work.history._lock
    original_raw, original_checkpoint = pending_service._raw, HistoryManager.checkpoint
    calls = []
    marker = "FakeRecoveryFault_DoNotExpose"

    def fingerprint(row):
        calls.append(True)
        # A batch also exercises a failure after one local fingerprint was prepared.
        if len(calls) == (2 if kind == "batch" else 1):
            raise RuntimeError(marker)
        return original_raw(row)

    def fork():
        calls.append(True)
        raise RuntimeError(marker)

    def checkpoint(staged, *args, **kwargs):
        assert staged is not history
        assert work.recovery_required and work_state(work) == before
        calls.append(True)
        if failure == "checkpoint_after":
            original_checkpoint(staged, *args, **kwargs)
        raise RuntimeError(marker)

    with monkeypatch.context() as scoped:
        if failure == "fingerprint":
            scoped.setattr(pending_service, "_raw", fingerprint)
        elif failure == "fork":
            scoped.setattr(history, "fork", fork)
        else:
            scoped.setattr(HistoryManager, "checkpoint", checkpoint)
        response = client.get(BASE + suffix, headers=admin_headers)
    assert calls and response.status_code == 500
    assert marker not in caplog.text + response.get_data(as_text=True)
    assert work_state(work) == before
    assert work.history is history and history._cipher is cipher and history._lock is lock
    assert database_state(db) == db_before
    assert client.get(BASE + suffix, headers=admin_headers).status_code == 200
    assert not work.recovery_required
    assert_active_rows_tracked(work)
    assert database_state(db) == db_before


@pytest.mark.parametrize("failure", ["fork", "baseline_before", "baseline_after"])
def test_failed_initial_reconcile_leaves_history_uninitialized(client, admin_headers, admin_user,
                                                              db, monkeypatch, failure):
    work = workspace(admin_user.id)
    work.recovery_required = True
    before, db_before = work_state(work), database_state(db)
    assert before[0][0] == {}
    history = work.history
    original_push, calls = HistoryManager.push_state, []

    def fork():
        calls.append(True)
        raise RuntimeError("FakeBaselineFault")

    def push(staged, *args, **kwargs):
        assert staged is not history
        calls.append(True)
        if failure == "baseline_after":
            original_push(staged, *args, **kwargs)
        raise RuntimeError("FakeBaselineFault")

    with monkeypatch.context() as scoped:
        if failure == "fork":
            scoped.setattr(history, "fork", fork)
        else:
            scoped.setattr(HistoryManager, "push_state", push)
        assert client.get(BASE, headers=admin_headers).status_code == 500
    assert calls == [True] and work_state(work) == before
    assert work.history is history
    assert database_state(db) == db_before
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert not work.recovery_required
    assert_active_rows_tracked(work)


def populated_manager(key=None, prefix="source"):
    manager = HistoryManager(backup_key=key)
    for value in (0, 1):
        manager.push_state({"draft": prefix, "value": value}, "operator", "EDIT", "RAM draft")
    manager.undo()
    return manager


def backup_for(manager, version):
    return legacy_11(manager) if version == "1.1" else json.loads(manager.export_backup_file())


@pytest.mark.parametrize("version", ["1.1", "1.2"])
@pytest.mark.parametrize("matching", [False, True])
def test_caller_key_restore_or_rejection_preserves_receiving_history(version, matching):
    key = Fernet.generate_key()
    source = populated_manager(key)
    target = populated_manager(key if matching else Fernet.generate_key(), "receiving")
    before = deepcopy(target._histories)
    backup = backup_for(source, version)
    if matching:
        assert target.restore_from_backup(backup) == source.current_snapshot()
        assert target.redo() == {"draft": "source", "value": 1}
        assert target._histories["default"]["states"][:len(before["default"]["states"])] == before["default"]["states"]
    else:
        with pytest.raises(ValueError):
            target.restore_from_backup(backup)
        assert target._histories == before


@pytest.mark.parametrize("version", ["1.1", "1.2"])
def test_new_manager_in_same_process_reuses_default_key(version):
    source = populated_manager()
    target = populated_manager(prefix="receiving")
    assert source._cipher is target._cipher
    assert target.restore_from_backup(backup_for(source, version)) == source.current_snapshot()


@pytest.mark.parametrize("version", ["1.1", "1.2"])
@pytest.mark.parametrize("mode", ["default", "caller_same", "caller_wrong"])
def test_fresh_process_key_result_and_failed_restore_atomicity(version, mode):
    key = None if mode == "default" else Fernet.generate_key().decode()
    source = populated_manager(key)
    target_key = Fernet.generate_key().decode() if mode == "caller_wrong" else key
    code = '''import sys,json,socket,sqlite3
from copy import deepcopy
sys.dont_write_bytecode=True
def blocked(*a,**k): raise AssertionError("Offline child prohibits network and DB")
for name in ("connect","connect_ex","sendto","bind","listen"):
    setattr(socket.socket,name,blocked)
socket.create_connection=socket.getaddrinfo=blocked
sqlite3.connect=sqlite3.dbapi2.connect=blocked
from network_copilot.history_extension import HistoryManager
data=json.loads(sys.stdin.read())
target=HistoryManager(backup_key=data["key"])
for n in (0,1): target.push_state({"receiving":n},"receiver","EDIT","RAM draft")
target.undo()
before=deepcopy(target._histories)
try:
    result=target.restore_from_backup(data["backup"])
    ok=data["matching"] and result=={"draft":"source","value":0} and target.can_redo()
except ValueError:
    ok=not data["matching"] and target._histories==before
print(json.dumps({"ok":bool(ok)}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], input=json.dumps({
        "key": target_key, "backup": backup_for(source, version), "matching": mode == "caller_same",
    }), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, "Offline key child failed; output intentionally withheld"
    assert json.loads(result.stdout) == {"ok": True}


@pytest.mark.parametrize("error,status", [(ValueError, 400), (ConflictError, 409),
                                         (ValidationError, 422), (RuntimeError, 500)])
def test_request_and_workspace_locks_release_after_exception(app, client, admin_headers, admin_user,
                                                           access_switch, db, monkeypatch, error, status):
    preview(client, admin_headers, access_switch)
    work = workspace(admin_user.id)
    before, db_before = work_state(work), database_state(db)
    request_lock = app.extensions["pending_history"]["request_lock"]

    def probe():
        for lock in (request_lock, work.lock):
            if not lock.acquire(blocking=False):
                return False
            lock.release()
        return True

    def fail(actor):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert not pool.submit(probe).result(timeout=3)
        raise error("Injected lock exception")

    with monkeypatch.context() as scoped:
        scoped.setattr(work, "synchronize", fail)
        assert client.get(BASE, headers=admin_headers).status_code == status
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(probe).result(timeout=3)
    assert work_state(work) == before and database_state(db) == db_before
    assert client.get(BASE, headers=admin_headers).status_code == 200


def test_second_request_waits_until_first_releases_request_lock(app, client, admin_headers, admin_user,
                                                              access_switch, monkeypatch):
    preview(client, admin_headers, access_switch)
    work = workspace(admin_user.id)
    first_entered, second_arrived, second_entered, release = (Event() for _ in range(4))
    original = work.synchronize

    def before_lock():
        if request.headers.get("X-History-Test") == "second":
            second_arrived.set()

    hooks = app.before_request_funcs[None]
    monkeypatch.setitem(app.before_request_funcs, None, [before_lock] + list(hooks))

    def synchronize(actor):
        if request.headers.get("X-History-Test") == "first":
            first_entered.set()
            assert release.wait(timeout=5), "Timed out waiting for test release"
        else:
            second_entered.set()
        return original(actor)

    monkeypatch.setattr(work, "synchronize", synchronize)

    def get(label):
        with app.test_client() as threaded_client:
            return threaded_client.get(BASE, headers={**admin_headers, "X-History-Test": label}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(get, "first")
        try:
            assert first_entered.wait(timeout=3)
            lock = app.extensions["pending_history"]["request_lock"]
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
            assert not acquired
            second = pool.submit(get, "second")
            assert second_arrived.wait(timeout=3)
            assert not second_entered.wait(timeout=0.1)
        finally:
            release.set()
        assert first.result(timeout=5) == 200
        assert second.result(timeout=5) == 200
    assert second_entered.is_set()
