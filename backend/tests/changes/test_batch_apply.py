import pytest
from types import SimpleNamespace
from sqlalchemy import update

from network_copilot.audit.model import AuditLog
from network_copilot.auth.model import User
from network_copilot.changes import batch_service
from network_copilot.changes.batch_service import BatchOperation
from network_copilot.changes.model import ChangeRequest
from network_copilot.devices import service as device_service
from network_copilot.errors import ConflictError, InvalidStateError, ValidationError
from network_copilot.extensions import db
from network_copilot.ssh.exceptions import SSHConnectionError


def make_write_batch(user_id, devices):
    return batch_service.create_batch_preview(
        user_id,
        [BatchOperation(
            [device.hostname for device in devices],
            "exec",
            ["write memory"],
            [],
        )],
        "Save configurations",
    )


def statuses(batch):
    return {change.device.hostname: change.status for change in batch.changes}


# -- approve_batch ----------------------------------------------------------


def test_approve_batch_moves_parent_and_children_to_approved(
    app, admin_user, access_switch, dist_switch
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    result = batch_service.approve_batch(batch.id, admin_user.id)

    assert result.status == "approved"
    assert result.approved_by_id == admin_user.id
    assert result.approved_at is not None
    assert all(change.status == "approved" for change in result.changes)


def test_only_pending_batches_can_be_approved(app, admin_user, access_switch):
    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    with pytest.raises(InvalidStateError):
        batch_service.approve_batch(batch.id, admin_user.id)


def test_approve_batch_records_a_batch_level_audit_event(
    app, admin_user, access_switch
):
    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.approve_batch(batch.id, admin_user.id)

    event = db.session.query(AuditLog).filter_by(action="batch.approve").one()
    assert event.result == "success"
    assert event.user_id == admin_user.id
    assert event.details == {
        "batch_id": batch.id,
        "child_count": 1,
        "risk_level": "high",
    }


# -- cancel_batch -------------------------------------------------------------


def test_cancel_batch_moves_parent_and_pending_children_to_cancelled(
    app, admin_user, access_switch, dist_switch
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    result = batch_service.cancel_batch(batch.id, admin_user.id)

    assert result.status == "cancelled"
    assert all(change.status == "cancelled" for change in result.changes)


def test_cancel_batch_also_cancels_approved_children(
    app, admin_user, access_switch, dist_switch
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.cancel_batch(batch.id, admin_user.id)

    assert result.status == "cancelled"
    assert all(change.status == "cancelled" for change in result.changes)


def test_cancel_batch_records_a_batch_level_audit_event(
    app, admin_user, access_switch
):
    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.cancel_batch(batch.id, admin_user.id)

    event = db.session.query(AuditLog).filter_by(action="batch.cancel").one()
    assert event.result == "success"
    assert event.user_id == admin_user.id
    assert event.details == {"batch_id": batch.id, "child_count": 1}


def test_cancelled_batch_cannot_be_applied(app, admin_user, access_switch, ssh_factory):
    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.cancel_batch(batch.id, admin_user.id)
    with pytest.raises(InvalidStateError):
        batch_service.apply_batch(batch.id, admin_user.id, confirmation="ACC-SW1")


# -- apply_batch: confirmation ------------------------------------------------


def test_dangerous_multi_device_batch_requires_confirm_all(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    with pytest.raises(ValidationError):
        batch_service.apply_batch(batch.id, admin_user.id, confirmation="ACC-SW1")
    assert ssh_factory.clients == {}


def test_dangerous_single_device_batch_requires_exact_hostname(
    app, admin_user, access_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    with pytest.raises(ValidationError):
        batch_service.apply_batch(batch.id, admin_user.id, confirmation="CONFIRM ALL")
    assert ssh_factory.clients == {}


def test_dangerous_single_device_batch_tolerates_whitespace_around_hostname(
    app, admin_user, access_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch])
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show startup-config": "hostname ACC-SW1",
        },
    )
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="  ACC-SW1  "
    )
    assert result.status == "success"


def test_confirm_all_tolerates_surrounding_whitespace(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show startup-config": "hostname ACC-SW1",
        },
    )
    ssh_factory.set_client(
        dist_switch.hostname,
        responses={
            "show running-config": "hostname DIST-SW1",
            "show startup-config": "hostname DIST-SW1",
        },
    )
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="  CONFIRM ALL  "
    )
    assert result.status == "success"


def test_confirmation_is_checked_before_any_ssh_work(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    """The confirmation gate must reject before touching a single child - not
    partway through the loop after some children already ran."""
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    ssh_factory.set_client(access_switch.hostname)
    ssh_factory.set_client(dist_switch.hostname)
    batch_service.approve_batch(batch.id, admin_user.id)

    with pytest.raises(ValidationError):
        batch_service.apply_batch(batch.id, admin_user.id, confirmation=None)

    assert ssh_factory.get(access_switch.hostname).calls == []
    assert ssh_factory.get(dist_switch.hostname).calls == []
    assert statuses(batch_service.get_batch(batch.id)) == {
        "ACC-SW1": "approved",
        "DIST-SW1": "approved",
    }


def test_live_connection_identity_mutation_fails_child_without_connecting(
    app, admin_user, access_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    access_switch.management_ip = "10.10.10.99"
    db.session.commit()

    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="ACC-SW1"
    )

    assert result.status == "failed"
    assert "identity changed" in result.changes[0].error_message.lower()
    assert ssh_factory.clients == {}


def test_deleted_target_fails_child_and_later_child_still_runs(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    ssh_factory.set_client(
        dist_switch.hostname,
        responses={
            "show running-config": "hostname DIST-SW1",
            "show startup-config": "hostname DIST-SW1",
        },
    )

    db.session.delete(access_switch)
    db.session.commit()

    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="CONFIRM ALL"
    )
    assert result.status == "partial_success"
    assert {change.target_hostname: change.status for change in result.changes} == {
        "ACC-SW1": "failed",
        "DIST-SW1": "success",
    }
    assert set(ssh_factory.clients) == {"DIST-SW1"}


def test_device_service_rejects_deleting_a_nonterminal_batch_target(
    app, admin_user, access_switch
):
    batch = make_write_batch(admin_user.id, [access_switch])

    with pytest.raises(ConflictError, match="active change batch"):
        device_service.delete_device(access_switch.id)

    assert batch_service.get_batch(batch.id).confirmation_text == "ACC-SW1"


# -- apply_batch: partial success continuation --------------------------------


def test_confirm_all_continues_after_one_device_fails(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    ssh_factory.set_failing(access_switch.hostname, SSHConnectionError("offline"))
    ssh_factory.set_client(
        dist_switch.hostname,
        responses={
            "show running-config": "hostname DIST-SW1",
            "show startup-config": "hostname DIST-SW1",
        },
    )
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="CONFIRM ALL"
    )
    assert result.status == "partial_success"
    assert statuses(result) == {"ACC-SW1": "failed", "DIST-SW1": "success"}
    assert ssh_factory.get(dist_switch.hostname).exec_batches == [["write memory"]]


def test_apply_batch_refreshes_child_state_between_committed_children(
    app, admin_user, access_switch, dist_switch, monkeypatch
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    children = sorted(batch.changes, key=lambda change: change.device.hostname)
    first_id, second_id = (change.id for change in children)
    observed_commands: dict[int, list[str]] = {}

    session = db.session()
    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False

    def apply_child(change, user_id):
        if change.id == first_id:
            statement = (
                update(ChangeRequest)
                .where(ChangeRequest.id == second_id)
                .values(commands=["fresh command from database"])
                .execution_options(synchronize_session=False)
            )
            db.session.execute(statement)
        observed_commands[change.id] = list(change.commands or [])
        change.status = "success"
        db.session.commit()
        return change

    monkeypatch.setattr(batch_service, "_apply_approved_change", apply_child)
    try:
        result = batch_service.apply_batch(
            batch.id, admin_user.id, confirmation="CONFIRM ALL"
        )
    finally:
        session.expire_on_commit = previous_expire_on_commit

    assert result.status == "success"
    assert observed_commands[second_id] == ["fresh command from database"]


def test_apply_batch_rolls_back_a_failed_transaction_before_continuing(
    app, admin_user, access_switch, dist_switch, monkeypatch
):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    children = sorted(batch.changes, key=lambda change: change.device.hostname)
    first_id = children[0].id
    attempted: list[int] = []

    def apply_child(change, user_id):
        attempted.append(change.id)
        if change.id == first_id:
            duplicate = User(
                username=admin_user.username,
                password_hash=admin_user.password_hash,
                role=admin_user.role,
            )
            db.session.add(duplicate)
            db.session.flush()
        change.status = "success"
        db.session.commit()
        return change

    monkeypatch.setattr(batch_service, "_apply_approved_change", apply_child)
    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="CONFIRM ALL"
    )

    assert attempted == [change.id for change in children]
    assert result.status == "partial_success"
    assert statuses(result) == {"ACC-SW1": "failed", "DIST-SW1": "success"}


def test_all_children_failing_marks_the_batch_failed(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    ssh_factory.set_failing(access_switch.hostname, SSHConnectionError("offline"))
    ssh_factory.set_failing(dist_switch.hostname, SSHConnectionError("offline"))
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="CONFIRM ALL"
    )
    assert result.status == "failed"
    assert statuses(result) == {"ACC-SW1": "failed", "DIST-SW1": "failed"}


def test_all_children_succeeding_marks_the_batch_success(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show startup-config": "hostname ACC-SW1",
        },
    )
    ssh_factory.set_client(
        dist_switch.hostname,
        responses={
            "show running-config": "hostname DIST-SW1",
            "show startup-config": "hostname DIST-SW1",
        },
    )
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="CONFIRM ALL"
    )
    assert result.status == "success"
    assert result.applied_at is not None


def test_apply_batch_audits_result_without_persisting_confirmation_text(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    for device in (access_switch, dist_switch):
        ssh_factory.set_client(
            device.hostname,
            responses={
                "show running-config": f"hostname {device.hostname}",
                "show startup-config": f"hostname {device.hostname}",
            },
        )
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="  CONFIRM ALL  "
    )

    event = db.session.query(AuditLog).filter_by(action="batch.apply").one()
    assert event.result == "success"
    assert event.user_id == admin_user.id
    assert event.details["batch_id"] == result.id
    assert event.details["outcomes"] == ["success", "success"]
    persisted_audit_text = str(
        [(item.message, item.details) for item in db.session.query(AuditLog).all()]
    )
    assert "CONFIRM ALL" not in persisted_audit_text


def test_apply_batch_sets_applied_at(
    app, admin_user, access_switch, ssh_factory
):
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show startup-config": "hostname ACC-SW1",
        },
    )
    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(batch.id, admin_user.id, confirmation="ACC-SW1")
    assert result.applied_at is not None


# -- apply_batch: low-risk batches need no confirmation at all -----------------


def test_low_risk_batch_applies_without_any_confirmation(
    app, admin_user, access_switch, ssh_factory
):
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show vlan brief": (
                "VLAN Name                             Status    Ports\n"
                "---- -------------------------------- --------- ---\n"
                "25   MARKETING                        active\n"
            ),
        },
        config_output="ACC-SW1(config-vlan)#",
    )
    batch = batch_service.create_batch_preview(
        admin_user.id,
        [BatchOperation(
            ["ACC-SW1"],
            "config",
            ["configure terminal", "vlan 25", "name MARKETING", "end"],
            [],
        )],
        "Low risk VLAN change",
    )
    assert batch.requires_confirmation is False

    batch_service.approve_batch(batch.id, admin_user.id)
    result = batch_service.apply_batch(batch.id, admin_user.id)
    assert result.status == "success"


# -- apply_batch: state ---------------------------------------------------------


def test_only_approved_batches_can_be_applied(app, admin_user, access_switch, ssh_factory):
    batch = make_write_batch(admin_user.id, [access_switch])
    with pytest.raises(InvalidStateError):
        batch_service.apply_batch(batch.id, admin_user.id, confirmation="ACC-SW1")
    assert ssh_factory.clients == {}


def test_standalone_lifecycle_functions_reject_batch_children(
    app, admin_user, access_switch, ssh_factory
):
    from network_copilot.changes import service as change_service

    batch = make_write_batch(admin_user.id, [access_switch])
    child = batch.changes[0]
    with pytest.raises(InvalidStateError, match="batch"):
        change_service.approve(child.id, admin_user.id)

    batch_service.approve_batch(batch.id, admin_user.id)
    with pytest.raises(InvalidStateError, match="batch"):
        change_service.apply(
            child.id, admin_user.id, confirm_hostname="ACC-SW1"
        )
    with pytest.raises(InvalidStateError, match="batch"):
        change_service.cancel(child.id, admin_user.id)
    assert ssh_factory.clients == {}


def test_internal_batch_apply_requires_approved_child_and_running_parent(
    app, admin_user, access_switch, ssh_factory
):
    from network_copilot.changes import service as change_service

    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.approve_batch(batch.id, admin_user.id)

    with pytest.raises(InvalidStateError, match="running"):
        change_service._apply_approved_change(batch.changes[0], admin_user.id)
    assert ssh_factory.clients == {}


def test_apply_batch_aborts_when_atomic_claim_loses_a_race(
    app, admin_user, access_switch, ssh_factory, monkeypatch
):
    batch = make_write_batch(admin_user.id, [access_switch])
    batch_service.approve_batch(batch.id, admin_user.id)
    original_execute = db.session.execute

    def lose_claim(statement, *args, **kwargs):
        if getattr(getattr(statement, "table", None), "name", None) == "change_batches":
            return SimpleNamespace(rowcount=0)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db.session, "execute", lose_claim)
    with pytest.raises(InvalidStateError, match="claimed"):
        batch_service.apply_batch(
            batch.id, admin_user.id, confirmation="ACC-SW1"
        )
    assert ssh_factory.clients == {}


# -- aggregate_status ---------------------------------------------------------


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([], "failed"),
        (["success", "success"], "success"),
        (["success", "failed"], "partial_success"),
        (["failed", "failed"], "failed"),
    ],
)
def test_aggregate_status(outcomes, expected):
    assert batch_service.aggregate_status(outcomes) == expected
