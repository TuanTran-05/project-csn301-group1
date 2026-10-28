"""Palo Alto PAN-OS through Preview -> Approve -> Apply (commit)."""

import pytest

from network_copilot.backups.model import ConfigBackup
from network_copilot.changes import service as change_service
from network_copilot.devices.model import Device
from network_copilot.errors import ValidationError
from network_copilot.extensions import db
from network_copilot.monitoring.service import commands_for_device
from network_copilot.ssh.exceptions import SSHCommandError

SET_LINES = [
    "set network interface ethernet ethernet1/2 layer3 ip 10.20.0.1/24",
    "set zone dmz network layer3 ethernet1/2",
]
RUNNING = "config {\n  devices {\n    localhost.localdomain {\n      deviceconfig { system { hostname PA-FW1; } }\n    }\n  }\n}\n"


@pytest.fixture
def firewall(project):
    device = Device(project_id=project.id, hostname="PA-FW1", management_ip="172.16.3.123",
                    device_type="paloalto_panos", role="firewall", kind="firewall",
                    environment="virtual", isolated=True)
    db.session.add(device)
    db.session.commit()
    return device


def test_preview_wraps_set_lines_in_configure_and_commit(admin_user, firewall):
    change = change_service.create_preview(
        admin_user.id, device_id=firewall.id,
        commands=["configure", *SET_LINES, "commit"],
    )
    assert change.commands == ["configure", *SET_LINES, "commit"]
    assert change.rollback_commands == [
        "configure",
        "delete network interface ethernet ethernet1/2 layer3 ip 10.20.0.1/24",
        "delete zone dmz network layer3 ethernet1/2",
        "commit",
    ]
    assert change.verification_commands == ["show config running"]
    assert change.capability_tier == "best_effort"
    assert any("commit" in warning for warning in change.warnings)


@pytest.mark.parametrize("commands,execution_mode", [
    (["interface ethernet1/2", "ip address 10.0.0.1 255.255.255.0"], "config"),  # IOS syntax
    (["configure", "commit"], "config"),                                          # nothing to do
    (["set zone dmz"], "exec"),
])
def test_preview_refuses_what_panos_cannot_run(admin_user, firewall, commands, execution_mode):
    with pytest.raises(ValidationError):
        change_service.create_preview(admin_user.id, device_id=firewall.id, commands=commands,
                                      execution_mode=execution_mode)


def test_a_delete_needs_the_typed_confirmation(admin_user, firewall):
    change = change_service.create_preview(
        admin_user.id, device_id=firewall.id, commands=["delete zone dmz"])
    assert change.requires_confirmation and change.risk_level == "high"
    assert change.rollback_commands[1].startswith("# restore")


def test_apply_backs_up_with_show_config_running_then_commits(admin_user, firewall, ssh_factory):
    client = ssh_factory.set_client(
        firewall.hostname,
        responses={"show config running": RUNNING},
        config_output="Configuration committed successfully",
    )
    change = change_service.create_preview(admin_user.id, device_id=firewall.id, commands=SET_LINES)
    change_service.approve(change.id, admin_user.id)
    applied = change_service.apply(change.id, admin_user.id)

    assert applied.status == "success", applied.error_message
    assert client.show_commands[0] == "show config running"
    assert client.config_batches == [["configure", *SET_LINES, "commit"]]
    assert db.session.get(ConfigBackup, applied.backup_id).running_config == RUNNING


def test_a_failed_commit_fails_the_change(admin_user, firewall, ssh_factory):
    client = ssh_factory.set_client(firewall.hostname, responses={"show config running": RUNNING})

    def failed_commit(commands):
        client.config_batches.append(list(commands))
        raise SSHCommandError(
            "PAN-OS commit failed on 172.16.3.123; the candidate configuration was "
            "reverted and nothing was applied."
        )

    client.run_config = failed_commit
    change = change_service.create_preview(admin_user.id, device_id=firewall.id, commands=SET_LINES)
    change_service.approve(change.id, admin_user.id)
    result = change_service.apply(change.id, admin_user.id)

    assert result.status == "failed"
    assert "commit failed" in result.error_message and "reverted" in result.error_message
    assert result.verification_output is None
    assert client.show_commands == ["show config running"]  # no verification after a failed commit


def test_monitoring_polls_panos_commands_only(firewall):
    assert commands_for_device(firewall) == ["show system info", "show interface all", "show routing route"]
