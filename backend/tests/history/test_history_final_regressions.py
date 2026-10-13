"""Final-review probes as regressions. Only suite SQLite RAM and fake data.

The offline runner additionally blocks all disk SQLite, sockets, SSH, Apply and
device verification before collection. No credential/snapshot is printed.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from network_copilot.changes.model import ChangeBatch, ChangeRequest
from network_copilot.history_extension import HistoryManager
from network_copilot.history_extension.pending_service import PendingWorkspace, workspace
from network_copilot.history_extension.protection import (
    BackupSizeError, MAX_BACKUP_BYTES, _legacy_audit_view, audit_view, decode_backup, encode_backup,
)

from .test_history_boundaries import prohibit_device_operations  # noqa: F401
from .test_history_hardening import journal, opened
from .test_pending_history import BASE, make_batch, move, preview


@pytest.fixture(autouse=True)
def confirm_ram_database(app, db):
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    assert db.engine.url.database == ":memory:"
    assert not app.config["MONITORING_ENABLED"]


def create_draft(client, headers, user, access, dist, kind):
    if kind == "change":
        data = preview(client, headers, access)
        return data["id"], [data["id"]]
    batch = make_batch(user, access, dist)
    return batch.id, [child.id for child in batch.changes]


def patch_for(editor, kind, description):
    body = {"revision": editor["revision"], "description": description}
    if kind == "batch":
        body["changes"] = [{"id": child["id"], "description": description} for child in editor["draft"]["changes"]]
    return body


def external_write(db, child_id):
    # First SELECTs have not started a SQLite write transaction. This simulates
    # a writer outside the request lock without triggering this Session's hooks.
    connection = db.session.connection().connection.driver_connection
    connection.execute("UPDATE change_requests SET commands=?, description=? WHERE id=?", (
        json.dumps(["configure terminal", "interface Gi0/2", "description external-new", "end"]),
        "external-new", child_id))
    connection.commit()


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("point", ["staging", "after_staging", "cas"])
def test_writer_between_revision_and_commit_is_conflict(client, admin_headers, admin_user,
                                                       access_switch, dist_switch, db, monkeypatch, kind, point):
    record_id, children = create_draft(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    url, editor = opened(client, admin_headers, kind, record_id)
    work = workspace(admin_user.id)
    before = journal(work)
    fired = []
    if point == "staging":
        original = PendingWorkspace.synchronize
        def injected(self, actor):
            if self is not work and not fired:
                fired.append(True)
                external_write(db, children[0])
            return original(self, actor)
        monkeypatch.setattr(PendingWorkspace, "synchronize", injected)
    elif point == "after_staging":
        original = PendingWorkspace.record
        def injected(self, *args):
            result = original(self, *args)
            if self is not work and not fired:
                fired.append(True)
                external_write(db, children[0])
            return result
        monkeypatch.setattr(PendingWorkspace, "record", injected)
    else:
        original = PendingWorkspace._cas
        def injected(self, row, values):
            if not fired:
                fired.append(True)
                external_write(db, children[0])
            return original(self, row, values)
        monkeypatch.setattr(PendingWorkspace, "_cas", injected)
    response = client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "editor-save"))
    assert fired and response.status_code == 409
    assert journal(work) == before
    db.session.expire_all()
    child = db.session.get(ChangeRequest, children[0])
    assert child.description == "external-new"
    assert "description external-new" in child.commands
    if kind == "batch":
        assert db.session.get(ChangeBatch, record_id).description == "first"
        assert db.session.get(ChangeRequest, children[1]).description == "first"


@pytest.mark.parametrize("kind", ["change", "batch"])
def test_writer_in_another_thread_during_staging_is_conflict(app, client, admin_headers, admin_user,
                                                           access_switch, dist_switch, db, monkeypatch, kind):
    record_id, children = create_draft(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    url, editor = opened(client, admin_headers, kind, record_id)
    work = workspace(admin_user.id)
    before = journal(work)
    original = PendingWorkspace.synchronize
    fired = []
    def write():
        with app.app_context():
            assert db.engine.url.database == ":memory:"
            row = db.session.get(ChangeRequest, children[0])
            row.description = "thread-write"
            db.session.commit()
    def injected(self, actor):
        if self is not work and not fired:
            fired.append(True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(write).result(timeout=5)
        return original(self, actor)
    monkeypatch.setattr(PendingWorkspace, "synchronize", injected)
    assert client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "editor-save")).status_code == 409
    assert journal(work) == before
    db.session.expire_all()
    assert db.session.get(ChangeRequest, children[0]).description == "thread-write"


def test_device_identity_changed_between_validation_and_sql_is_conflict(client, admin_headers, admin_user,
                                                                      access_switch, db, monkeypatch):
    created = preview(client, admin_headers, access_switch)
    url, editor = opened(client, admin_headers, "change", created["id"])
    work = workspace(admin_user.id)
    before = journal(work)
    original = PendingWorkspace._cas
    device_id = access_switch.id
    fired = []
    def injected(self, row, values):
        if not fired:
            fired.append(True)
            connection = db.session.connection().connection.driver_connection
            connection.execute("UPDATE devices SET device_type='cisco_asa' WHERE id=?", (device_id,))
            connection.commit()
        return original(self, row, values)
    monkeypatch.setattr(PendingWorkspace, "_cas", injected)
    assert client.patch(url, headers=admin_headers, json=patch_for(editor, "change", "new")).status_code == 409
    assert journal(work) == before
    db.session.expire_all()
    assert db.session.get(ChangeRequest, created["id"]).description == "first"


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("operation", ["edit", "undo", "redo", "restore"])
@pytest.mark.parametrize("fault", ["response", "precommit_read", "before_commit", "postcommit_read", "listener", "wrapper"])
def test_commit_boundary_keeps_database_and_history_consistent(client, admin_headers, admin_user,
                                                             access_switch, dist_switch, db, monkeypatch,
                                                             caplog, kind, operation, fault):
    record_id, children = create_draft(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    url, editor = opened(client, admin_headers, kind, record_id)
    assert client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "second")).status_code == 200
    if operation == "redo":
        move(client, admin_headers, "undo")
    work = workspace(admin_user.id)
    before = journal(work)
    before_description = "first" if operation == "redo" else "second"
    expected_description = "third" if operation == "edit" else ("second" if operation == "redo" else "first")
    _, fresh = opened(client, admin_headers, kind, record_id)
    committed, written, reads_after = [], [], []
    original_commit = db.session.commit
    original_read = PendingWorkspace.read
    original_cas = PendingWorkspace._cas
    session = db.session()
    fake_error = "FakeFaultCredential_DoNotExpose123"

    def fail(*args):
        raise RuntimeError(fake_error)

    def commit_wrapper():
        if fault == "before_commit":
            fail()
        result = original_commit()
        committed.append(True)
        if fault == "wrapper":
            fail()
        return result
    monkeypatch.setattr(db.session, "commit", commit_wrapper)

    def read(self):
        if committed:
            reads_after.append(True)
            fail()
        if fault == "precommit_read" and written:
            fail()
        return original_read(self)
    monkeypatch.setattr(PendingWorkspace, "read", read)

    def cas(self, row, values):
        result = original_cas(self, row, values)
        written.append(True)
        return result
    monkeypatch.setattr(PendingWorkspace, "_cas", cas)
    if fault == "response":
        original_status = HistoryManager.status
        def status(self, *args, **kwargs):
            if self is not work.history:
                fail()
            return original_status(self, *args, **kwargs)
        monkeypatch.setattr(HistoryManager, "status", status)
    if fault == "listener":
        event.listen(session, "after_commit", fail)
    try:
        if operation == "edit":
            response = client.patch(url, headers=admin_headers, json=patch_for(fresh, kind, "third"))
        else:
            response = client.post(BASE + "/" + operation, headers=admin_headers,
                                   **({"json": backup} if operation == "restore" else {}))
    finally:
        if fault == "listener":
            event.remove(session, "after_commit", fail)
    assert fake_error not in caplog.text + response.get_data(as_text=True)
    successful = fault in {"postcommit_read", "listener", "wrapper"}
    assert response.status_code == (200 if successful else 500)
    assert not reads_after
    if successful:
        assert journal(work) != before
        assert work.history.current_snapshot()["changes"][0]["description"] == expected_description
        assert response.get_json()["snapshot"] == work.history.current_snapshot()
    else:
        assert journal(work) == before
    db.session.expire_all()
    description = expected_description if successful else before_description
    assert all(db.session.get(ChangeRequest, child).description == description for child in children)
    if kind == "batch":
        assert db.session.get(ChangeBatch, record_id).description == description


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("operation", ["edit", "undo", "redo", "restore"])
def test_driver_error_after_physical_commit_recovers_publication(client, admin_headers, admin_user,
                                                               access_switch, dist_switch, db, monkeypatch,
                                                               kind, operation):
    record_id, children = create_draft(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    url, editor = opened(client, admin_headers, kind, record_id)
    assert client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "second")).status_code == 200
    if operation == "redo":
        move(client, admin_headers, "undo")
    work = workspace(admin_user.id)
    before = journal(work)
    _, editor = opened(client, admin_headers, kind, record_id)
    expected = "third" if operation == "edit" else ("second" if operation == "redo" else "first")
    original = db.engine.dialect.do_commit
    committed = []
    def driver(connection):
        original(connection)
        committed.append(True)
        raise RuntimeError("Injected post-driver-commit fault")
    with monkeypatch.context() as scoped:
        scoped.setattr(db.engine.dialect, "do_commit", driver)
        response = client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "third")) if operation == "edit" else (
            client.post(BASE + "/" + operation, headers=admin_headers,
                        **({"json": backup} if operation == "restore" else {})))
    assert committed and response.status_code == 200
    assert journal(work) != before and not work.recovery_required
    assert work.history.current_snapshot()["changes"][0]["description"] == expected
    db.session.expire_all()
    assert all(db.session.get(ChangeRequest, child).description == expected for child in children)
    if kind == "batch":
        assert db.session.get(ChangeBatch, record_id).description == expected


def test_unreadable_commit_outcome_blocks_mutations_until_explicit_refresh(client, admin_headers, admin_user,
                                                                        access_switch, db, monkeypatch):
    created = preview(client, admin_headers, access_switch)
    url, editor = opened(client, admin_headers, "change", created["id"])
    work = workspace(admin_user.id)
    before = journal(work)
    original_commit = db.engine.dialect.do_commit
    original_read = PendingWorkspace.read
    committed = []
    def driver(connection):
        original_commit(connection)
        committed.append(True)
        raise RuntimeError("Injected physical commit fault")
    def unreadable(self):
        if committed:
            raise RuntimeError("Injected unavailable outcome query")
        return original_read(self)
    with monkeypatch.context() as scoped:
        scoped.setattr(db.engine.dialect, "do_commit", driver)
        scoped.setattr(PendingWorkspace, "read", unreadable)
        response = client.patch(url, headers=admin_headers, json=patch_for(editor, "change", "committed"))
    assert response.status_code == 409
    assert response.get_json()["details"]["reason"] == "commit_outcome_unknown"
    assert work.recovery_required and journal(work) == before
    assert client.post(BASE + "/undo", headers=admin_headers).status_code == 409
    assert client.patch(url, headers=admin_headers, json=patch_for(editor, "change", "must-not-write")).status_code == 409
    assert journal(work) == before
    response = client.get(BASE, headers=admin_headers)
    assert response.status_code == 200 and not work.recovery_required
    assert response.get_json()["snapshot"]["changes"][0]["description"] == "committed"
    assert db.session.get(ChangeRequest, created["id"]).description == "committed"


@pytest.mark.parametrize("bad", ["malformed", "key", "cipher", "metadata", "audit", "conflict"])
def test_failed_restore_does_not_checkpoint_live_history(client, admin_headers, admin_user, access_switch, db, bad):
    created = preview(client, admin_headers, access_switch)
    preview(client, admin_headers, access_switch, "second")
    move(client, admin_headers, "undo")
    work = workspace(admin_user.id)
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    if bad == "malformed":
        backup = {}
    elif bad == "key":
        wrong = work.history.fork()
        wrong._cipher = Fernet(Fernet.generate_key())
        backup = json.loads(wrong.export_backup_file())
    elif bad == "cipher":
        backup["encrypted_history"] = backup["encrypted_history"][:-5] + "XXXXX"
    elif bad == "metadata":
        backup["audit_trail"][0]["actor"] = "modified"
    elif bad == "audit":
        backup = work.history.export_audit_log()
    external_write(db, created["id"])
    before = journal(work)
    assert work.history.can_redo()
    response = client.post(BASE + "/restore", headers=admin_headers, json=backup)
    assert response.status_code == (409 if bad == "conflict" else 400)
    assert journal(work) == before and work.history.can_redo()
    db.session.expire_all()
    assert db.session.get(ChangeRequest, created["id"]).description == "external-new"


def test_invalid_restore_on_uninitialized_workspace_does_not_create_baseline(client, admin_headers, admin_user):
    work = workspace(admin_user.id)
    before = journal(work)
    assert client.post(BASE + "/restore", headers=admin_headers, json={}).status_code == 400
    assert journal(work) == before


def test_restore_untracked_owned_record_leaves_empty_history_untouched(client, admin_headers, admin_user,
                                                                    access_switch, db):
    from network_copilot.changes.service import prepare_change
    # A pre-existing draft, created directly in the suite RAM DB, has no history hook.
    row = prepare_change(admin_user.id, access_switch, ["interface Gi0/2", "description first"], [], "first", "manual")
    db.session.add(row)
    db.session.commit()
    work = workspace(admin_user.id)
    before = journal(work)
    source = HistoryManager()
    source.push_state(work.read()[0], "operator", "BASELINE", "initial")
    response = client.post(BASE + "/restore", headers=admin_headers, json=json.loads(source.export_backup_file()))
    assert response.status_code == 403
    assert journal(work) == before
    assert db.session.get(ChangeRequest, row.id).description == "first"


def test_sqlite_busy_and_unverified_backend_are_conflicts(client, admin_headers, admin_user, access_switch, db, monkeypatch):
    created = preview(client, admin_headers, access_switch)
    url, editor = opened(client, admin_headers, "change", created["id"])
    work = workspace(admin_user.id)
    before = journal(work)
    def busy():
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise OperationalError("", {}, error)
    with monkeypatch.context() as scoped:
        scoped.setattr(db.session, "commit", busy)
        assert client.patch(url, headers=admin_headers, json=patch_for(editor, "change", "new")).status_code == 409
    assert journal(work) == before
    with monkeypatch.context() as scoped:
        scoped.setattr(db.session, "get_bind", lambda: SimpleNamespace(dialect=SimpleNamespace(name="unverified")))
        assert client.patch(url, headers=admin_headers, json=patch_for(editor, "change", "new")).status_code == 409
    assert journal(work) == before
    assert db.session.get(ChangeRequest, created["id"]).description == "first"


def test_public_audit_and_diff_never_copy_opaque_metadata_or_keys():
    fake = "FakeOpaqueCredential_NeverPrint123"
    manager = HistoryManager()
    manager.push_state({fake: fake, "opaque_config": fake, "commands": [fake]}, fake, fake, fake, fake)
    manager.push_state({fake: fake + "next", "opaque_config": fake + "next"}, fake, "EDIT", fake, fake)
    backup = manager.export_backup_file(fake)
    assert fake not in backup
    assert fake not in json.dumps(manager.export_audit_log(fake))
    assert fake not in json.dumps(manager.get_diff(fake))
    assert "topology_id" not in json.loads(backup)
    restored = HistoryManager()
    result = restored.restore_from_backup(backup, fake)
    assert result == manager.current_snapshot(fake)
    assert restored.undo(fake)["opaque_config"] == fake


def test_public_header_projection_checks_values_and_invalid_private_data_is_rejected():
    fake = "FakeOpaqueHeaderCredential_NeverPrint123"
    manager = HistoryManager()
    manager.push_state({fake: fake}, fake, "BASELINE", fake)
    private = decode_backup(json.loads(manager.export_backup_file()), manager._cipher)
    private.update({"exported_at": fake, "key_scope": fake, "topology_id": fake, fake: fake})
    private["history_state"] = {"undo_stack": [fake, {fake: fake}], fake: fake}
    private["audit_trail"][0]["step"] = fake
    assert fake not in json.dumps(audit_view(private))
    target = HistoryManager()
    with pytest.raises(ValueError) as error:
        target.restore_from_backup(encode_backup(private, manager._cipher))
    assert fake not in str(error.value)
    assert target.current_snapshot() is None


def legacy_11(manager):
    private = decode_backup(json.loads(manager.export_backup_file()), manager._cipher)
    private.pop("key_scope", None)
    public = _legacy_audit_view(private)
    public["encrypted_history"] = manager._cipher.encrypt(json.dumps(private).encode()).decode()
    return public


def test_existing_11_backup_retains_key_and_exact_restore():
    manager = HistoryManager()
    manager.push_state({"commands": ["FakeCommandCredential123"]}, "operator", "BASELINE", "initial")
    old = legacy_11(manager)
    assert old["backup_version"] == "1.1"
    restored = HistoryManager()
    assert restored.restore_from_backup(old) == manager.current_snapshot()
    assert json.loads(restored.export_backup_file())["backup_version"] == "1.2"


@pytest.mark.parametrize("durable", [False, True])
def test_fresh_process_requires_same_caller_key(durable):
    key = Fernet.generate_key().decode() if durable else None
    manager = HistoryManager(backup_key=key)
    manager.push_state({"commands": ["FakePrivateCommand123"]}, "operator", "BASELINE", "initial")
    code = '''import json,sys,socket,sqlite3
sys.dont_write_bytecode=True
def blocked(*a,**k): raise AssertionError("Offline child: network and database prohibited")
socket.socket.connect=socket.socket.connect_ex=socket.socket.sendto=blocked
socket.create_connection=socket.getaddrinfo=blocked
sqlite3.connect=sqlite3.dbapi2.connect=blocked
from network_copilot.history_extension import HistoryManager
data=json.loads(sys.stdin.read())
try:
    restored=HistoryManager(backup_key=data["key"])
    snapshot=restored.restore_from_backup(data["backup"])
    print(json.dumps({"restored": snapshot == {"commands": ["FakePrivateCommand123"]}}))
except ValueError:
    print(json.dumps({"restored": False}))
'''
    child = subprocess.run([sys.executable, "-B", "-c", code],
                           input=json.dumps({"key": key, "backup": json.loads(manager.export_backup_file())}),
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 0
    assert json.loads(child.stdout)["restored"] is durable


def test_export_import_actual_size_boundary_and_utf8():
    def candidate(length):
        manager = HistoryManager()
        manager.push_state({"text": "x" * length}, "operator", "BASELINE", "initial")
        return manager
    lower, upper = 0, MAX_BACKUP_BYTES
    while lower + 1 < upper:
        middle = (lower + upper) // 2
        try:
            candidate(middle).export_backup_file()
            lower = middle
        except BackupSizeError:
            upper = middle
    backup = candidate(lower).export_backup_file()
    size = len(backup.encode("utf-8"))
    assert 0 <= MAX_BACKUP_BYTES - size < 32
    restored = HistoryManager()
    assert len(restored.restore_from_backup(backup)["text"]) == lower
    with pytest.raises(BackupSizeError):
        candidate(upper).export_backup_file()
    padded = backup + " " * (MAX_BACKUP_BYTES - size)
    assert len(HistoryManager().restore_from_backup(padded)["text"]) == lower
    with pytest.raises(BackupSizeError):
        restored.restore_from_backup(padded + " ")
    assert len(restored.current_snapshot()["text"]) == lower
    # Limit uses bytes, not character count.
    with pytest.raises(BackupSizeError):
        restored.restore_from_backup("\u20ac" * (MAX_BACKUP_BYTES // 3 + 1))


def test_size_errors_are_structured_and_no_oversized_export_is_returned(client, admin_headers):
    base = "/api/draft-history"
    for _ in range(2):
        response = client.post(base + "/states", headers=admin_headers, json={
            "snapshot": {"text": "x" * (MAX_BACKUP_BYTES // 2)}, "action_type": "EDIT", "description": "initial"})
        assert response.status_code == 201
    response = client.get(base + "/backup", headers=admin_headers)
    assert response.status_code == 422 and response.get_json()["error"] == "backup_too_large"
    assert "encrypted_history" not in response.get_json()
    for route in (base, BASE):
        response = client.post(route + "/restore", headers=admin_headers,
                               data=b"x" * (MAX_BACKUP_BYTES + 1), content_type="application/json")
        assert response.status_code == 422 and response.get_json()["error"] == "backup_too_large"
