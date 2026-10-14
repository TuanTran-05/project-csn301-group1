"""State publication faults. Run only with the pre-collection offline guard."""
from copy import deepcopy

import pytest

from network_copilot.changes.model import ChangeRequest
from network_copilot.history_extension import HistoryManager
from network_copilot.history_extension import pending_service

from .test_history_boundaries import prohibit_device_operations  # noqa: F401
from .test_history_final_regressions import create_draft, patch_for
from .test_history_hardening import journal, opened
from .test_history_recovery_hooks import (
    BASE, confirm_ram_only, database_state, draft_with_redo, work_state,
)


def assert_ready(work):
    state = work.history._state
    snapshot, rows = work.read()
    assert work._matches(state, snapshot, rows)
    assert not state.needs_reconcile
    assert work._attempt is None and not work.recovery_required


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("suffix", ["", "/backup", "/audit"])
@pytest.mark.parametrize("fault", ["history", "fingerprints", "revision", "before_publish", "after_publish", "ack"])
def test_synchronize_fault_keeps_one_generation_then_recovers(client, admin_headers, admin_user,
                                                            access_switch, dist_switch, db,
                                                            monkeypatch, caplog, kind, suffix, fault):
    record_id, children, work = draft_with_redo(client, admin_headers, admin_user,
                                               access_switch, dist_switch, kind)
    url, old_editor = opened(client, admin_headers, kind, record_id)
    work.expected.pop(("change", children[0]))
    work.recovery_required = True
    manager, cipher, lock = work.history, work.history._cipher, work.history._lock
    base, before, db_before = manager._state, work_state(work), database_state(db)
    original_checkpoint, original_install = HistoryManager.checkpoint, HistoryManager._install_state
    marker, calls = "FakePublicationFault_DoNotExpose", []

    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError(marker)

    def history(candidate, *args, **kwargs):
        assert candidate is not manager
        original_checkpoint(candidate, *args, **kwargs)
        fail()

    def install(current_manager, old, target):
        if current_manager is manager:
            calls.append(True)
            assert work._attempt.target is target
            assert target.generation == base.generation + 1
            assert target.revision_epoch == base.revision_epoch + 1
            assert target.histories is not base.histories
            if fault == "after_publish":
                original_install(current_manager, old, target)
            raise RuntimeError(marker)
        return original_install(current_manager, old, target)

    with monkeypatch.context() as scoped:
        if fault == "history":
            scoped.setattr(HistoryManager, "checkpoint", history)
        elif fault == "fingerprints":
            scoped.setattr(pending_service, "_raw", fail)
        elif fault == "revision":
            # The packet constructor runs after history/tracking preparation.
            scoped.setattr(pending_service, "HistoryState", fail)
        elif fault in {"before_publish", "after_publish"}:
            scoped.setattr(HistoryManager, "_install_state", install)
        else:
            scoped.setattr(work, "_acknowledge", fail)
        response = client.get(BASE + suffix, headers=admin_headers)
    assert response.status_code == 500 and calls == [True]
    assert marker not in caplog.text + response.get_data(as_text=True)
    assert work.recovery_required and database_state(db) == db_before
    assert work.history is manager and manager._cipher is cipher and manager._lock is lock
    if fault in {"after_publish", "ack"}:
        assert manager._state is work._attempt.target
        assert manager._state.revision_epoch == base.revision_epoch + 1
        assert manager._state.generation == base.generation + 1
        assert work._matches(manager._state, *work.read())
    else:
        assert manager._state is base and work_state(work) == before
    if fault in {"before_publish", "after_publish", "ack"}:
        assert work._attempt is not None
    else:
        assert work._attempt is None

    assert client.get(BASE + suffix, headers=admin_headers).status_code == 200
    assert_ready(work)
    ready = manager._state
    assert ready.generation == base.generation + 1
    assert ready.revision_epoch == base.revision_epoch + 1
    previous, current = before[0][0]["default"], ready.histories["default"]
    for field in ("audit", "events", "states"):
        assert current[field][:-1] == previous[field]
    assert current["events"][-1]["action_type"] == "CHECKPOINT"
    assert current["undo"] == [len(current["events"]) - 1] and current["redo"] == []
    assert not manager.can_undo() and not manager.can_redo()
    assert database_state(db) == db_before
    for _ in range(2):
        assert client.get(BASE + suffix, headers=admin_headers).status_code == 200
        assert manager._state is ready and database_state(db) == db_before
    stale = client.patch(url, headers=admin_headers, json=patch_for(old_editor, kind, "stale"))
    assert stale.status_code == 409
    assert stale.get_json()["details"]["reason"] == "stale_revision"
    assert manager._state is ready and database_state(db) == db_before
    _, fresh = opened(client, admin_headers, kind, record_id)
    assert fresh["revision"] != old_editor["revision"]
    assert client.patch(url, headers=admin_headers, json=patch_for(fresh, kind, "fresh")).status_code == 200
    assert all(db.session.get(ChangeRequest, child).description == "fresh" for child in children)
    assert_ready(work)
    assert work.history is manager and manager._cipher is cipher and manager._lock is lock


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("operation", ["edit", "undo", "redo", "restore"])
@pytest.mark.parametrize("fault", ["before_publish", "after_publish"])
@pytest.mark.parametrize("persistent", [False, True])
def test_committed_callback_retries_same_packet_without_duplicate_events(client, admin_headers, admin_user,
                                                                        access_switch, dist_switch, db,
                                                                        monkeypatch, caplog, kind,
                                                                        operation, fault, persistent):
    record_id, children, work = draft_with_redo(client, admin_headers, admin_user,
                                               access_switch, dist_switch, kind)
    url, editor = opened(client, admin_headers, kind, record_id)
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    manager, cipher, lock = work.history, work.history._cipher, work.history._lock
    base = manager._state
    original_install, attempts = HistoryManager._install_state, []
    marker = "FakeCommitPublicationFault_DoNotExpose"

    def install(current_manager, old, target):
        if current_manager is manager:
            assert work._attempt.source == "committed_db"
            attempts.append(work._attempt)
            if fault == "after_publish":
                original_install(current_manager, old, target)
            if persistent or len(attempts) == 1:
                raise RuntimeError(marker)
            if fault == "before_publish":
                original_install(current_manager, old, target)
            return
        return original_install(current_manager, old, target)

    with monkeypatch.context() as scoped:
        scoped.setattr(HistoryManager, "_install_state", install)
        if operation == "edit":
            response = client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "fresh"))
        else:
            response = client.post(BASE + "/" + operation, headers=admin_headers,
                                   **({"json": backup} if operation == "restore" else {}))
    assert len(attempts) == 2 and attempts[0] is attempts[1]
    receipt = attempts[0]
    assert receipt.base is base and receipt.target.generation == base.generation + 1
    assert marker not in caplog.text + response.get_data(as_text=True)
    assert work._matches(receipt.target, *work.read())  # The draft DB commit happened.
    db_committed = database_state(db)
    if persistent:
        assert response.status_code == 409 and work.recovery_required
        assert work._attempt is receipt
        assert manager._state is (base if fault == "before_publish" else receipt.target)
    else:
        assert response.status_code == 200
        assert manager._state is receipt.target
        assert_ready(work)
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert manager._state is receipt.target
    assert_ready(work)
    for _ in range(2):
        assert client.get(BASE, headers=admin_headers).status_code == 200
        assert manager._state is receipt.target
    assert database_state(db) == db_committed
    assert manager._state.histories == receipt.target.histories
    assert work.history is manager and manager._cipher is cipher and manager._lock is lock
    # No checkpoint is needed for a known committed packet, including Restore.
    assert sum(e["action_type"] == "CHECKPOINT" for e in manager._histories["default"]["events"]) == sum(
        e["action_type"] == "CHECKPOINT" for e in base.histories["default"]["events"])
    if operation == "undo":
        # Undo cancelled this draft; reopen it only through the ordinary Redo path.
        assert client.post(BASE + "/redo", headers=admin_headers).status_code == 200
    stable, db_stable = manager._state, database_state(db)
    stale = client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "stale"))
    assert stale.status_code == 409 and stale.get_json()["details"]["reason"] == "stale_revision"
    assert manager._state is stable and database_state(db) == db_stable
    _, fresh = opened(client, admin_headers, kind, record_id)
    assert client.patch(url, headers=admin_headers, json=patch_for(fresh, kind, "after-recovery")).status_code == 200
    assert all(db.session.get(ChangeRequest, child).description == "after-recovery" for child in children)
    assert_ready(work)
    assert work.history is manager and manager._cipher is cipher and manager._lock is lock


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("after", [False, True])
def test_external_db_change_after_committed_fault_retains_event_and_one_barrier(client, admin_headers,
                                                                              admin_user, access_switch,
                                                                              dist_switch, db, monkeypatch,
                                                                              kind, after):
    record_id, children, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    url, editor = opened(client, admin_headers, kind, record_id)
    manager, base = work.history, work.history._state
    install = HistoryManager._install_state
    def fault(current_manager, old, target):
        if current_manager is manager:
            if after:
                install(current_manager, old, target)
            raise RuntimeError("FakeRepeatedCommitFault")
        return install(current_manager, old, target)
    with monkeypatch.context() as scoped:
        scoped.setattr(HistoryManager, "_install_state", fault)
        assert client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "committed")).status_code == 409
    committed = work._attempt.target
    db.session.get(ChangeRequest, children[0]).description = "external-current"
    db.session.commit()
    external = database_state(db)
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert_ready(work)
    ready = manager._state
    old, current = committed.histories["default"], ready.histories["default"]
    for field in ("audit", "events", "states"):
        assert current[field][:-1] == old[field]
    assert current["events"][-1]["action_type"] == "CHECKPOINT"
    assert ready.revision_epoch == committed.revision_epoch + 1
    assert current["undo"] == [len(current["events"]) - 1] and current["redo"] == []
    assert database_state(db) == external
    for _ in range(2):
        assert client.get(BASE, headers=admin_headers).status_code == 200
        assert manager._state is ready and database_state(db) == external
    assert base.histories["default"]["events"] == old["events"][:-1]


@pytest.mark.parametrize("kind", ["change", "batch"])
def test_packet_preparation_failure_before_db_commit_is_nonmutating(client, admin_headers, admin_user,
                                                                   access_switch, dist_switch, db, monkeypatch, kind):
    record_id, _, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    url, editor = opened(client, admin_headers, kind, record_id)
    base, before, db_before = work.history._state, work_state(work), database_state(db)
    def fail(*args, **kwargs):
        raise RuntimeError("FakePacketPreparationFault")
    monkeypatch.setattr(work, "_prepare_publication", fail)
    assert client.patch(url, headers=admin_headers, json=patch_for(editor, kind, "blocked")).status_code == 500
    assert work.history._state is base and work_state(work) == before
    assert database_state(db) == db_before and work._attempt is None


@pytest.mark.parametrize("kind", ["change", "batch"])
def test_fork_packet_and_candidates_do_not_alias_live_state(client, admin_headers, admin_user,
                                                          access_switch, dist_switch, monkeypatch, kind):
    _, _, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    base, before = work.history._state, journal(work)
    def no_cipher_copy(*args):
        raise AssertionError("Cipher must not be deepcopied")
    monkeypatch.setattr(type(work.history._cipher), "__deepcopy__", no_cipher_copy, raising=False)
    staged = work._fork()
    assert staged.history._state is not base
    assert staged.history._state.histories is not base.histories
    assert staged.expected is not base.expected
    assert staged.history._cipher is work.history._cipher
    assert staged.history._lock is not work.history._lock
    staged.expected.clear()
    staged.history._histories["default"]["redo"].clear()
    assert journal(work) == before
    receipt = work._prepare_publication(work.history, work.expected, work.revision_epoch + 1)
    assert receipt.target.histories is not base.histories and receipt.target.expected is not base.expected
    receipt.target.expected.clear()
    receipt.target.histories["default"]["undo"].clear()
    assert journal(work) == before


@pytest.mark.parametrize("operation", ["push", "checkpoint", "undo", "redo", "restore"])
@pytest.mark.parametrize("after", [False, True])
def test_standalone_manager_fault_observes_old_or_complete_new_state(monkeypatch, operation, after):
    manager = HistoryManager()
    for value in (0, 1):
        manager.push_state({"value": value}, "operator", "EDIT", "fake draft")
    if operation == "redo":
        manager.undo()
    backup = manager.export_backup_file()
    base, original_install, targets = manager._state, manager._install_state, []
    cipher, lock = manager._cipher, manager._lock
    def install(old, target):
        targets.append(target)
        if after:
            original_install(old, target)
        raise RuntimeError("FakeStandalonePublicationFault")
    with monkeypatch.context() as scoped:
        scoped.setattr(manager, "_install_state", install)
        with pytest.raises(RuntimeError):
            if operation == "push":
                manager.push_state({"value": 2}, "operator", "EDIT", "fake draft")
            elif operation == "checkpoint":
                manager.checkpoint({"value": 2}, "operator", "barrier")
            elif operation == "restore":
                manager.restore_from_backup(backup)
            else:
                getattr(manager, operation)()
    assert len(targets) == 1
    assert manager._state is (targets[0] if after else base)
    state = manager._state
    assert state.expected is None and not state.needs_reconcile
    for history in state.histories.values():
        assert len(history["events"]) == len(history["states"]) == len(history["audit"])
        assert all(0 <= i < len(history["states"]) for i in history["undo"] + history["redo"])
    original_install(base, targets[0])
    original_install(base, targets[0])
    assert manager._state is targets[0] and manager._cipher is cipher and manager._lock is lock


@pytest.mark.parametrize("kind", ["change", "batch"])
@pytest.mark.parametrize("writer", ["expected", "revision", "history"])
def test_each_generation_owns_history_and_tracking_mappings(client, admin_headers, admin_user,
                                                           access_switch, dist_switch, kind, writer):
    _, _, work = draft_with_redo(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    manager, base = work.history, work.history._state
    histories, expected = deepcopy(base.histories), deepcopy(base.expected)
    if writer == "expected":
        work.expected = work.expected
    elif writer == "revision":
        work.revision_epoch += 1
    else:
        manager.push_state(manager.current_snapshot(), "operator", "EDIT", "fake generation")
    target = manager._state
    assert target is not base and target.generation == base.generation + 1
    assert target.histories is not base.histories and target.expected is not base.expected
    # Mutating an obsolete generation must not change the live one.
    base.histories.clear()
    base.expected.clear()
    assert target.expected == expected
    if writer != "history":
        assert target.histories == histories
    else:
        assert target.histories["default"]["events"][:-1] == histories["default"]["events"]
    assert target.needs_reconcile and work.recovery_required
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert_ready(work)


@pytest.mark.parametrize("kind", ["change", "batch"])
def test_core_record_publications_do_not_share_old_generation_data(client, admin_headers, admin_user,
                                                                 access_switch, dist_switch, monkeypatch, kind):
    # Capture publications, then assert outside the fail-soft hook so an
    # AssertionError inside that hook cannot accidentally hide a test failure.
    from network_copilot.history_extension.pending_service import workspace
    work = workspace(admin_user.id)
    manager, install, pairs = work.history, HistoryManager._install_state, []
    def observed(current_manager, old, target):
        if current_manager is manager:
            pairs.append((old, target))
        return install(current_manager, old, target)
    monkeypatch.setattr(HistoryManager, "_install_state", observed)
    create_draft(client, admin_headers, admin_user, access_switch, dist_switch, kind)
    assert len(pairs) >= 3
    for old, target in pairs:
        assert target.histories is not old.histories
        assert target.expected is not old.expected
    assert_ready(work)
