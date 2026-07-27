import pytest

from network_copilot.backups.model import ConfigBackup
from network_copilot.changes import service as change_service
from network_copilot.errors import InvalidStateError
from network_copilot.extensions import db
from network_copilot.ssh.exceptions import SSHConnectionError

VLAN_COMMANDS = ["configure terminal", "vlan 25", "name MARKETING", "end"]

RUNNING_CONFIG = """Building configuration...

Current configuration : 1423 bytes
!
hostname ACC-SW1
!
end
"""

VLAN_AFTER = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/3
25   MARKETING                        active
"""

VLAN_BEFORE = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/3
"""


@pytest.fixture
def pending_change(app, admin_user, access_switch):
    return change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        commands=VLAN_COMMANDS,
        verification_commands=["show vlan brief"],
    )


@pytest.fixture
def applying_client(ssh_factory, access_switch):
    """SSH stub that reports the VLAN as present after the change."""
    return ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": RUNNING_CONFIG,
            "show vlan brief": VLAN_AFTER,
        },
        config_output="ACC-SW1(config-vlan)#",
    )


# -- approve --------------------------------------------------------------


def test_approve_moves_change_to_approved(app, admin_user, pending_change):
    change = change_service.approve(pending_change.id, admin_user.id)
    assert change.status == "approved"
    assert change.approved_by_id == admin_user.id
    assert change.approved_at is not None


def test_only_pending_changes_can_be_approved(app, admin_user, pending_change):
    change_service.approve(pending_change.id, admin_user.id)
    with pytest.raises(InvalidStateError):
        change_service.approve(pending_change.id, admin_user.id)


def test_approve_endpoint_requires_admin(client, viewer_headers, pending_change):
    response = client.post(
        f"/api/changes/{pending_change.id}/approve", headers=viewer_headers
    )
    assert response.status_code == 403


def test_approve_endpoint(client, admin_headers, pending_change):
    response = client.post(
        f"/api/changes/{pending_change.id}/approve", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "approved"


# -- cancel ---------------------------------------------------------------


def test_cancel_moves_change_to_cancelled(app, admin_user, pending_change):
    change = change_service.cancel(pending_change.id, admin_user.id)
    assert change.status == "cancelled"


def test_cancelled_change_cannot_be_applied(
    app, admin_user, pending_change, applying_client
):
    change_service.cancel(pending_change.id, admin_user.id)
    with pytest.raises(InvalidStateError):
        change_service.apply(pending_change.id, admin_user.id)


def test_cancel_endpoint_requires_admin(client, viewer_headers, pending_change):
    response = client.post(
        f"/api/changes/{pending_change.id}/cancel", headers=viewer_headers
    )
    assert response.status_code == 403


# -- apply ordering -------------------------------------------------------


def test_apply_runs_backup_then_config_then_verification(
    app, admin_user, pending_change, applying_client
):
    change_service.approve(pending_change.id, admin_user.id)
    change_service.apply(pending_change.id, admin_user.id)

    kinds = [call[0] for call in applying_client.calls]
    assert kinds == ["run_show", "run_config", "run_show"]
    assert applying_client.calls[0][1] == "show running-config"
    assert applying_client.calls[1][1] == VLAN_COMMANDS
    assert applying_client.calls[2][1] == "show vlan brief"


def test_apply_stores_a_backup_before_the_change(
    app, admin_user, pending_change, applying_client
):
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)

    backup = db.session.get(ConfigBackup, change.backup_id)
    assert backup is not None
    assert "hostname ACC-SW1" in backup.running_config
    assert backup.change_request_id == change.id
    assert backup.device_id == change.device_id


def test_successful_apply_is_marked_success(
    app, admin_user, pending_change, applying_client
):
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)
    assert change.status == "success"
    assert change.applied_at is not None
    assert change.error_message is None


def test_apply_requires_approval_first(app, admin_user, pending_change, applying_client):
    with pytest.raises(InvalidStateError):
        change_service.apply(pending_change.id, admin_user.id)
    assert applying_client.calls == []


def test_a_change_cannot_be_applied_twice(
    app, admin_user, pending_change, applying_client
):
    change_service.approve(pending_change.id, admin_user.id)
    change_service.apply(pending_change.id, admin_user.id)
    with pytest.raises(InvalidStateError):
        change_service.apply(pending_change.id, admin_user.id)


# -- verification ---------------------------------------------------------


def test_verification_confirms_vlan_id_and_name(
    app, admin_user, pending_change, applying_client
):
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)
    verification = change.verification_output["show vlan brief"]
    assert verification["passed"] is True
    assert "MARKETING" in verification["output"]


def test_failed_verification_marks_the_change_failed(
    app, admin_user, pending_change, ssh_factory, access_switch
):
    # The device reports the VLAN was never created.
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": RUNNING_CONFIG,
            "show vlan brief": VLAN_BEFORE,
        },
    )
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)

    assert change.status == "failed"
    assert change.error_message
    assert change.verification_output["show vlan brief"]["passed"] is False


def test_verification_failure_still_records_the_backup(
    app, admin_user, pending_change, ssh_factory, access_switch
):
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": RUNNING_CONFIG,
            "show vlan brief": VLAN_BEFORE,
        },
    )
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)
    assert db.session.get(ConfigBackup, change.backup_id) is not None


def test_ssh_failure_during_apply_marks_the_change_failed(
    app, admin_user, pending_change, ssh_factory, access_switch
):
    ssh_factory.set_failing(access_switch.hostname, SSHConnectionError("link down"))
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)

    assert change.status == "failed"
    assert "link down" in change.error_message


def test_apply_aborts_when_the_backup_cannot_be_taken(
    app, admin_user, pending_change, ssh_factory, access_switch
):
    fake = ssh_factory.set_failing(access_switch.hostname, SSHConnectionError("down"))
    change_service.approve(pending_change.id, admin_user.id)
    change_service.apply(pending_change.id, admin_user.id)
    # Nothing was configured because the pre-change backup never succeeded.
    assert fake.config_batches == []


def test_rollback_commands_are_reported_but_never_executed(
    app, admin_user, pending_change, ssh_factory, access_switch
):
    fake = ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": RUNNING_CONFIG,
            "show vlan brief": VLAN_BEFORE,
        },
    )
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)

    assert change.status == "failed"
    assert change.rollback_commands
    # The MVP surfaces rollback commands; it never runs them automatically.
    assert len(fake.config_batches) == 1
    assert fake.config_batches[0] == VLAN_COMMANDS


# -- apply endpoint -------------------------------------------------------


def test_apply_endpoint_requires_admin(client, viewer_headers, pending_change):
    response = client.post(
        f"/api/changes/{pending_change.id}/apply", headers=viewer_headers
    )
    assert response.status_code == 403


def test_apply_endpoint_rejects_unapproved_changes(
    client, admin_headers, pending_change, applying_client
):
    response = client.post(
        f"/api/changes/{pending_change.id}/apply", headers=admin_headers
    )
    assert response.status_code == 409


def test_apply_endpoint_full_flow(
    client, admin_headers, pending_change, applying_client
):
    client.post(f"/api/changes/{pending_change.id}/approve", headers=admin_headers)
    response = client.post(
        f"/api/changes/{pending_change.id}/apply", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    assert body["backup_id"] is not None


def test_apply_endpoint_404s_for_unknown_change(client, admin_headers):
    assert (
        client.post("/api/changes/999/apply", headers=admin_headers).status_code == 404
    )


def test_backups_endpoint_lists_device_backups(
    client, admin_headers, admin_user, pending_change, applying_client, access_switch
):
    change_service.approve(pending_change.id, admin_user.id)
    change_service.apply(pending_change.id, admin_user.id)
    response = client.get(
        f"/api/devices/{access_switch.id}/backups", headers=admin_headers
    )
    assert response.status_code == 200
    assert len(response.get_json()["items"]) == 1
