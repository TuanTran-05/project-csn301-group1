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
    # Backup, write, verify, then the post-change backup that becomes the
    # baseline for the next preview.
    assert kinds == ["run_show", "run_config", "run_show", "run_show"]
    assert applying_client.calls[0][1] == "show running-config"
    assert applying_client.calls[1][1] == VLAN_COMMANDS
    assert applying_client.calls[2][1] == "show vlan brief"
    assert applying_client.calls[3][1] == "show running-config"


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
    assert change.error_message == (
        "Verification failed; the device does not reflect the requested change. "
        "Review rollback_commands and apply them manually if required."
    )
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


def test_backup_ssh_failure_keeps_the_stage_specific_error_message(
    app, admin_user, pending_change, ssh_factory, access_switch
):
    ssh_factory.set_failing(
        access_switch.hostname,
        SSHConnectionError("Could not connect to g1lab@10.0.0.9:22."),
    )
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)

    assert change.status == "failed"
    assert change.error_message.startswith("Pre-change backup failed: ")
    assert access_switch.hostname in change.error_message
    # The reason an apply stopped is read by whoever asked for it, so it
    # gets the same treatment as chat: the device, not the SSH account.
    assert "g1lab" not in change.error_message


def test_configuration_ssh_failure_keeps_the_stage_specific_error_message(
    app, admin_user, pending_change, applying_client, monkeypatch
):
    def fail_configuration(commands):
        raise SSHConnectionError("write channel closed")

    monkeypatch.setattr(applying_client, "run_config", fail_configuration)
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)

    assert change.status == "failed"
    assert change.error_message.startswith("Applying configuration failed: ")
    assert "không kết nối được" in change.error_message.lower()


def test_verification_ssh_failure_keeps_the_stage_specific_error_message(
    app, admin_user, pending_change, applying_client, monkeypatch
):
    original_run_show = applying_client.run_show

    def fail_verification(command):
        if command == "show vlan brief":
            raise SSHConnectionError("verification channel closed")
        return original_run_show(command)

    monkeypatch.setattr(applying_client, "run_show", fail_verification)
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)

    assert change.status == "failed"
    assert change.error_message.startswith("Verification could not run: ")
    assert "không kết nối được" in change.error_message.lower()


def test_client_construction_failure_marks_standalone_change_failed(
    app, admin_user, pending_change, monkeypatch
):
    from network_copilot.ssh import client as ssh_client

    def fail_client_construction(device):
        raise RuntimeError("credentials unavailable")

    monkeypatch.setattr(
        ssh_client, "build_client_for_device", fail_client_construction
    )
    change_service.approve(pending_change.id, admin_user.id)
    change = change_service.apply(pending_change.id, admin_user.id)

    assert change.status == "failed"
    assert change.error_message == (
        "Could not open an SSH session: credentials unavailable"
    )


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


def test_apply_endpoint_requires_confirm_hostname_for_flagged_changes(
    client, admin_headers, access_switch, ssh_factory
):
    ssh_factory.set_client(access_switch.hostname)
    change_id = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={
            "device_id": access_switch.id,
            "execution_mode": "exec",
            "commands": ["write erase"],
        },
    ).get_json()["id"]
    client.post(f"/api/changes/{change_id}/approve", headers=admin_headers)

    response = client.post(
        f"/api/changes/{change_id}/apply", headers=admin_headers
    )
    assert response.status_code == 422


def test_apply_endpoint_rejects_a_wrong_confirm_hostname(
    client, admin_headers, access_switch, ssh_factory
):
    ssh_factory.set_client(access_switch.hostname)
    change_id = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={
            "device_id": access_switch.id,
            "execution_mode": "exec",
            "commands": ["write erase"],
        },
    ).get_json()["id"]
    client.post(f"/api/changes/{change_id}/approve", headers=admin_headers)

    response = client.post(
        f"/api/changes/{change_id}/apply",
        headers=admin_headers,
        json={"confirm_hostname": "WRONG-NAME"},
    )
    assert response.status_code == 422


def test_apply_endpoint_succeeds_with_the_correct_confirm_hostname(
    client, admin_headers, access_switch, ssh_factory
):
    ssh_factory.set_client(access_switch.hostname, config_output="ok", default_output="ok")
    change_id = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={
            "device_id": access_switch.id,
            "execution_mode": "exec",
            "commands": ["write erase"],
        },
    ).get_json()["id"]
    client.post(f"/api/changes/{change_id}/approve", headers=admin_headers)

    response = client.post(
        f"/api/changes/{change_id}/apply",
        headers=admin_headers,
        json={"confirm_hostname": access_switch.hostname},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "success"


def test_apply_never_reaches_ssh_without_confirmation(
    client, admin_headers, access_switch, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    change_id = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={
            "device_id": access_switch.id,
            "execution_mode": "exec",
            "commands": ["write erase"],
        },
    ).get_json()["id"]
    client.post(f"/api/changes/{change_id}/approve", headers=admin_headers)
    client.post(f"/api/changes/{change_id}/apply", headers=admin_headers)

    assert fake.calls == []


def test_apply_without_confirmation_does_not_change_status(
    app, admin_user, access_switch, ssh_factory
):
    from network_copilot.errors import ValidationError

    ssh_factory.set_client(access_switch.hostname)
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        execution_mode="exec",
        commands=["write erase"],
    )
    change_service.approve(change.id, admin_user.id)

    with pytest.raises(ValidationError):
        change_service.apply(change.id, admin_user.id)

    assert change.status == "approved"


def test_apply_service_accepts_the_correct_confirm_hostname(
    app, admin_user, access_switch, ssh_factory
):
    ssh_factory.set_client(access_switch.hostname, config_output="ok", default_output="ok")
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        execution_mode="exec",
        commands=["write erase"],
    )
    change_service.approve(change.id, admin_user.id)

    result = change_service.apply(
        change.id, admin_user.id, confirm_hostname=access_switch.hostname
    )
    assert result.status == "success"


def test_apply_service_trims_confirmation_hostname_whitespace(
    app, admin_user, access_switch, ssh_factory
):
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show startup-config": "hostname ACC-SW1",
        },
    )
    change = change_service.create_preview(
        admin_user.id,
        device_id=access_switch.id,
        execution_mode="exec",
        commands=["write memory"],
    )
    change_service.approve(change.id, admin_user.id)

    result = change_service.apply(
        change.id, admin_user.id, confirm_hostname="  ACC-SW1  "
    )
    assert result.status == "success"


def test_apply_service_does_not_require_confirmation_for_ordinary_changes(
    app, admin_user, pending_change, applying_client
):
    change_service.approve(pending_change.id, admin_user.id)
    result = change_service.apply(pending_change.id, admin_user.id)
    assert result.status == "success"


def test_backups_endpoint_lists_device_backups(
    client, admin_headers, admin_user, pending_change, applying_client, access_switch
):
    change_service.approve(pending_change.id, admin_user.id)
    change_service.apply(pending_change.id, admin_user.id)
    response = client.get(
        f"/api/devices/{access_switch.id}/backups", headers=admin_headers
    )
    assert response.status_code == 200
    # The pre-change backup and the post-change one that refreshes the baseline.
    assert sorted(item["reason"] for item in response.get_json()["items"]) == ["post_change", "pre_change"]


# -- EXEC mode apply --------------------------------------------------------


def test_apply_exec_change_calls_run_exec_without_wrappers(
    app, admin_user, access_switch, ssh_factory
):
    fake = ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show startup-config": "hostname ACC-SW1",
        },
    )
    change = change_service.create_preview(
        admin_user.id, access_switch.id, ["write memory"], execution_mode="exec"
    )
    change_service.approve(change.id, admin_user.id)
    change_service.apply(change.id, admin_user.id, confirm_hostname="ACC-SW1")
    assert ("run_exec", ["write memory"]) in fake.calls
    assert fake.config_batches == []



def test_a_second_change_after_apply_needs_no_new_baseline(
    app, admin_user, access_switch, applying_client
):
    """The post-change backup becomes the next preview's baseline, so a second
    change applies without re-capturing the baseline by hand."""
    from network_copilot.backups.service import capture_backup

    access_switch.environment = "pnetlab"
    access_switch.isolated = False
    db.session.commit()
    write = applying_client.run_config

    def write_and_change_running_config(commands, **kwargs):
        # Every write leaves a different running-config behind.
        applying_client.responses["show running-config"] += "! " + " ".join(commands) + "\n"
        return write(commands, **kwargs)

    applying_client.run_config = write_and_change_running_config
    capture_backup(access_switch, reason="approval_baseline")

    for _ in range(2):
        change = change_service.create_preview(
            admin_user.id, access_switch.id, VLAN_COMMANDS, ["show vlan brief"]
        )
        change_service.approve(change.id, admin_user.id)
        applied = change_service.apply(change.id, admin_user.id)
        assert applied.status == "success", applied.error_message


TRUNK_COMMANDS = [
    "interface GigabitEthernet0/1",
    "switchport mode trunk",
    "switchport trunk allowed vlan 10,20,30",
]


def test_pnetlab_switch_trunk_gets_dot1q_before_trunk_mode(app, admin_user, access_switch):
    """IOSv-L2 refuses trunk mode while the encapsulation is "Auto"."""
    access_switch.environment = "pnetlab"
    db.session.commit()
    change = change_service.create_preview(admin_user.id, access_switch.id, TRUNK_COMMANDS)
    assert change.commands == [
        "configure terminal",
        "interface GigabitEthernet0/1",
        "switchport trunk encapsulation dot1q",
        "switchport mode trunk",
        "switchport trunk allowed vlan 10,20,30",
        "end",
    ]
    # Still recognised as a trunk change, so the trunk itself is verified.
    assert change.operation_families == ["trunk_port"]
    assert [item["id"] for item in change.verification_plan] == ["trunk_port:GigabitEthernet0/1"]


def test_an_explicit_trunk_encapsulation_is_kept(app, admin_user, access_switch):
    access_switch.environment = "pnetlab"
    db.session.commit()
    commands = ["interface Gi0/1", "switchport trunk encapsulation isl", "switchport mode trunk"]
    change = change_service.create_preview(admin_user.id, access_switch.id, commands)
    assert change.commands[1:-1] == commands


def test_other_switches_are_left_alone(app, admin_user, access_switch):
    """A Catalyst 2960 only speaks dot1q and answers the command with
    "% Invalid input", which would fail the whole batch."""
    change = change_service.create_preview(admin_user.id, access_switch.id, TRUNK_COMMANDS)
    assert change.commands[1:-1] == TRUNK_COMMANDS


def test_renaming_a_pnetlab_device_pins_ssh_to_a_named_key_first(app, admin_user, access_switch):
    """An unlabelled RSA key is named hostname.domain; IOS turns SSH off when
    either changes, cutting the session the rename runs over."""
    access_switch.environment = "pnetlab"
    db.session.commit()
    change = change_service.create_preview(
        admin_user.id, access_switch.id, ["hostname EIU-S1", "ip domain-name lab.eiu.local"]
    )
    assert change.commands[1:3] == [
        "crypto key generate rsa general-keys label NETAI-SSH modulus 2048",
        "ip ssh rsa keypair-name NETAI-SSH",
    ]
    assert change.commands[3:5] == ["hostname EIU-S1", "ip domain-name lab.eiu.local"]


def test_a_device_already_pinned_to_a_key_is_renamed_as_asked(app, admin_user, access_switch):
    access_switch.environment = "pnetlab"
    db.session.add(ConfigBackup(
        device_id=access_switch.id, reason="approval_baseline",
        running_config="hostname ACC-SW1\nip ssh rsa keypair-name NETAI-SSH\n",
    ))
    db.session.commit()
    change = change_service.create_preview(admin_user.id, access_switch.id, ["hostname EIU-S1"])
    assert change.commands == ["configure terminal", "hostname EIU-S1", "end"]
