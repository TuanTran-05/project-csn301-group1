import pytest

from network_copilot.changes import batch_service
from network_copilot.changes.batch_service import BatchOperation
from network_copilot.errors import InvalidStateError, ValidationError
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


# -- aggregate_status ---------------------------------------------------------


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [(["success", "success"], "success"), (["success", "failed"], "partial_success"), (["failed", "failed"], "failed")],
)
def test_aggregate_status(outcomes, expected):
    assert batch_service.aggregate_status(outcomes) == expected
