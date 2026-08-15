import pytest

from network_copilot.commands.model import CommandExecution
from network_copilot.extensions import db
from network_copilot.commands.service import execute_readonly
from network_copilot.errors import PolicyViolationError
from network_copilot.extensions import db

IFACE_OUTPUT = """Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0     10.255.0.2      YES NVRAM  up                    up
"""


def test_readonly_command_executes_and_returns_output(
    client, admin_headers, device, ssh_factory
):
    ssh_factory.set_client(
        device.hostname, responses={"show ip interface brief": IFACE_OUTPUT}
    )
    response = client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip interface brief"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    assert "GigabitEthernet0/0" in body["output"]
    assert body["duration_ms"] >= 0


def test_viewer_can_run_readonly_commands(client, viewer_headers, device, ssh_factory):
    ssh_factory.set_client(device.hostname, default_output="ok")
    response = client.post(
        "/api/commands/execute-readonly",
        headers=viewer_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )
    assert response.status_code == 200


def test_anonymous_cannot_run_commands(client, device):
    response = client.post(
        "/api/commands/execute-readonly",
        json={"device_id": device.id, "command": "show ip route"},
    )
    assert response.status_code == 401


def test_blocked_command_is_rejected_and_never_reaches_ssh(
    client, admin_headers, device, ssh_factory
):
    fake = ssh_factory.set_client(device.hostname, default_output="should not run")
    response = client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "write erase"},
    )
    assert response.status_code == 403
    assert fake.show_commands == []


def test_blocked_command_is_recorded(client, admin_headers, device, ssh_factory):
    ssh_factory.set_client(device.hostname)
    client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "reload"},
    )
    record = db.session.query(CommandExecution).one()
    assert record.status == "blocked"
    assert record.command == "reload"


def test_ai_source_blocks_running_config_before_ssh(app, device, ssh_factory):
    with app.app_context():
        with pytest.raises(PolicyViolationError):
            execute_readonly(
                device_id=device.id,
                command="show running-config",
                source="ai",
            )

        record = db.session.query(CommandExecution).one()
        assert record.status == "blocked"
        assert record.source == "ai"
        assert ssh_factory.clients == {}


def test_execution_is_persisted_with_context(
    client, admin_headers, admin_user, device, ssh_factory
):
    ssh_factory.set_client(device.hostname, default_output="output text")
    client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )
    record = db.session.query(CommandExecution).one()
    assert record.command == "show ip route"
    assert record.output == "output text"
    assert record.status == "success"
    assert record.device_id == device.id
    assert record.user_id == admin_user.id
    assert record.duration_ms >= 0
    assert record.created_at is not None


def test_ssh_failure_is_recorded_as_failed(client, admin_headers, device, ssh_factory):
    from network_copilot.ssh.exceptions import SSHConnectionError

    ssh_factory.set_failing(device.hostname, SSHConnectionError("host down"))
    response = client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )
    assert response.status_code == 502
    record = db.session.query(CommandExecution).one()
    assert record.status == "failed"


def test_ssh_failure_names_the_device_not_the_ssh_account(
    client, admin_headers, device, ssh_factory
):
    """What comes back to chat must identify the device and say what to
    check, without the "user@host:port" the SSH layer words its errors with."""
    from network_copilot.ssh.exceptions import SSHConnectionError

    ssh_factory.set_failing(
        device.hostname, SSHConnectionError("Could not connect to g1lab@10.0.0.9:22.")
    )
    response = client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )

    message = response.get_json()["message"]
    assert device.hostname in message
    assert "kiểm tra" in message.lower()
    assert "g1lab" not in message
    assert "@" not in message


def test_ssh_failure_keeps_the_raw_detail_in_the_audit_trail(
    client, admin_headers, device, ssh_factory
):
    """An admin diagnosing the failure still needs the transport detail.
    Friendlier wording in chat must not cost the audit trail its evidence."""
    from network_copilot.audit.model import AuditLog
    from network_copilot.ssh.exceptions import SSHConnectionError

    raw = "Could not connect to g1lab@10.0.0.9:22."
    ssh_factory.set_failing(device.hostname, SSHConnectionError(raw))
    client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )

    event = (
        db.session.query(AuditLog)
        .filter_by(action="command.readonly", result="failure")
        .one()
    )
    assert event.message == raw


def test_an_authentication_failure_does_not_claim_the_device_is_offline(
    client, admin_headers, device, ssh_factory
):
    from network_copilot.ssh.exceptions import SSHAuthenticationError

    ssh_factory.set_failing(
        device.hostname,
        SSHAuthenticationError("Authentication failed for g1lab@10.0.0.9:22."),
    )
    response = client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )

    message = response.get_json()["message"].lower()
    assert "đăng nhập" in message
    assert "không kết nối được" not in message


def test_unknown_device_returns_404(client, admin_headers):
    response = client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": 999, "command": "show ip route"},
    )
    assert response.status_code == 404


def test_missing_command_returns_422(client, admin_headers, device):
    response = client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id},
    )
    assert response.status_code == 422


def test_role_restricted_command_is_blocked(
    client, admin_headers, access_switch, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    response = client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": access_switch.id, "command": "show ip ospf neighbor"},
    )
    assert response.status_code == 403
    assert fake.show_commands == []


def test_history_returns_past_executions(client, admin_headers, device, ssh_factory):
    ssh_factory.set_client(device.hostname, default_output="ok")
    for command in ["show ip route", "show vlan brief"]:
        client.post(
            "/api/commands/execute-readonly",
            headers=admin_headers,
            json={"device_id": device.id, "command": command},
        )
    response = client.get("/api/commands/history", headers=admin_headers)
    assert response.status_code == 200
    assert len(response.get_json()["items"]) == 2


def test_history_can_be_filtered_by_device(
    client, admin_headers, device, access_switch, ssh_factory
):
    ssh_factory.set_client(device.hostname, default_output="ok")
    ssh_factory.set_client(access_switch.hostname, default_output="ok")
    client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )
    client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": access_switch.id, "command": "show vlan brief"},
    )
    response = client.get(
        f"/api/commands/history?device_id={access_switch.id}", headers=admin_headers
    )
    items = response.get_json()["items"]
    assert len(items) == 1
    assert items[0]["command"] == "show vlan brief"


def test_history_requires_authentication(client):
    assert client.get("/api/commands/history").status_code == 401
