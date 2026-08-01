"""End-to-end walk through the demo flow, with SSH and the AI provider faked.

login -> list devices -> run show command -> AI creates VLAN preview
-> approve -> backup -> apply -> verify -> audit
"""

import pytest
from fakes.fake_ai_provider import FakeAIProvider

from network_copilot.audit.model import AuditLog
from network_copilot.backups.model import ConfigBackup
from network_copilot.extensions import db
from network_copilot.ssh.exceptions import SSHConnectionError

IFACE_OUTPUT = """Interface                  IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0         10.255.0.2      YES NVRAM  up                    up
GigabitEthernet0/1         10.10.10.31     YES NVRAM  up                    up
"""

RUNNING_CONFIG = """Building configuration...

Current configuration : 1423 bytes
!
hostname ACC-SW1
!
end
"""

VLAN_BEFORE = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/3
"""

VLAN_AFTER = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/3
25   MARKETING                        active
"""

AI_VLAN_ACTION = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["ACC-SW1"],
        "execution_mode": "config",
        "commands": ["configure terminal", "vlan 25", "name MARKETING", "end"],
        "verification_commands": ["show vlan brief"],
    }],
    "explanation": "Creating VLAN 25 named MARKETING on ACC-SW1.",
}

AI_WRITE_ERASE_ACTION = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["ACC-SW1"],
        "execution_mode": "exec",
        "commands": ["write erase"],
        "verification_commands": [],
    }],
    "explanation": "Wiping the configuration.",
}

WRITE_ALL_ACTION = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["*"],
        "execution_mode": "exec",
        "commands": ["write memory"],
        "verification_commands": [],
    }],
    "explanation": "Luu cau hinh tren tat ca thiet bi.",
}


class StatefulSwitch:
    """Fake ACC-SW1 whose VLAN table actually changes when configured."""

    def __init__(self):
        self.vlan_output = VLAN_BEFORE
        self.show_commands: list[str] = []
        self.config_batches: list[list[str]] = []
        self.calls: list[tuple[str, object]] = []

    def test_connection(self) -> bool:
        self.calls.append(("test_connection", None))
        return True

    def run_show(self, command: str):
        from network_copilot.ssh.types import SSHResult

        self.calls.append(("run_show", command))
        self.show_commands.append(command)
        outputs = {
            "show ip interface brief": IFACE_OUTPUT,
            "show running-config": RUNNING_CONFIG,
            "show vlan brief": self.vlan_output,
        }
        return SSHResult(
            command=command, output=outputs.get(command, "ok"), duration_ms=1
        )

    def run_config(self, commands: list[str]):
        from network_copilot.ssh.types import SSHResult

        self.calls.append(("run_config", list(commands)))
        self.config_batches.append(list(commands))
        if "vlan 25" in commands:
            self.vlan_output = VLAN_AFTER
        return SSHResult(
            command="\n".join(commands), output="ACC-SW1(config)#", duration_ms=1
        )

    def close(self) -> None:
        pass


@pytest.fixture
def lab(app, admin_user, access_switch):
    switch = StatefulSwitch()
    app.config["SSH_CLIENT_FACTORY"] = lambda device: switch
    return switch


def test_complete_demo_flow(client, app, lab, access_switch, admin_user):
    # 1. Login.
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "StrongPass123!"}
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.get_json()['access_token']}"}

    # 2. List devices.
    devices = client.get("/api/devices", headers=headers)
    assert devices.status_code == 200
    assert any(item["hostname"] == "ACC-SW1" for item in devices.get_json()["items"])

    # 3. Run a read-only show command.
    show = client.post(
        "/api/commands/execute-readonly",
        headers=headers,
        json={"device_id": access_switch.id, "command": "show ip interface brief"},
    )
    assert show.status_code == 200
    assert "GigabitEthernet0/1" in show.get_json()["output"]

    # 4. The AI turns a sentence into a Preview. Nothing is configured yet.
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=AI_VLAN_ACTION)
    chat = client.post(
        "/api/ai/chat",
        headers=headers,
        json={"message": "Tao VLAN 25 MARKETING tren ACC-SW1"},
    )
    assert chat.status_code == 200
    body = chat.get_json()
    assert body["requires_approval"] is True
    batch_id = body["batch"]["id"]
    change_id = body["batch"]["changes"][0]["id"]
    assert body["batch"]["status"] == "pending_approval"
    assert lab.config_batches == []

    # 5. Approve.
    approve = client.post(f"/api/change-batches/{batch_id}/approve", headers=headers)
    assert approve.status_code == 200
    assert approve.get_json()["status"] == "approved"

    # 6 & 7. Apply: backup, configure, verify.
    applied = client.post(f"/api/change-batches/{batch_id}/apply", headers=headers)
    assert applied.status_code == 200
    result = applied.get_json()["changes"][0]
    assert result["status"] == "success"

    assert lab.show_commands[1] == "show running-config"
    assert lab.config_batches == [
        ["configure terminal", "vlan 25", "name MARKETING", "end"]
    ]
    assert lab.show_commands[-1] == "show vlan brief"

    backup = db.session.get(ConfigBackup, result["backup_id"])
    assert "hostname ACC-SW1" in backup.running_config

    verification = result["verification_output"]["show vlan brief"]
    assert verification["passed"] is True
    assert "MARKETING" in verification["output"]

    # 8. A dangerous AI request is not blocked - it still only creates a
    # Preview, flagged for confirmation, and never touches SSH by itself.
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(
        responses=AI_WRITE_ERASE_ACTION
    )
    dangerous = client.post(
        "/api/ai/chat", headers=headers, json={"message": "write erase ACC-SW1"}
    )
    assert dangerous.status_code == 200
    dangerous_batch = dangerous.get_json()["batch"]
    assert dangerous_batch["status"] == "pending_approval"
    assert dangerous_batch["requires_confirmation"] is True
    assert lab.config_batches == [
        ["configure terminal", "vlan 25", "name MARKETING", "end"]
    ]

    # Applying it without the confirmation is rejected, and still never
    # reaches the device.
    client.post(
        f"/api/change-batches/{dangerous_batch['id']}/approve", headers=headers
    )
    unconfirmed_apply = client.post(
        f"/api/change-batches/{dangerous_batch['id']}/apply", headers=headers
    )
    assert unconfirmed_apply.status_code == 422
    assert lab.config_batches == [
        ["configure terminal", "vlan 25", "name MARKETING", "end"]
    ]

    # 9. The audit trail covers the whole flow.
    logs = client.get("/api/audit-logs", headers=headers)
    actions = {item["action"] for item in logs.get_json()["items"]}
    assert {
        "auth.login",
        "command.readonly",
        "ai.action",
        "batch.approve",
        "batch.apply",
        "change.apply",
    } <= actions


def test_flow_never_leaks_credentials(client, app, lab, access_switch, admin_user):
    from network_copilot.credentials.service import store_device_credential

    with app.app_context():
        store_device_credential(access_switch.id, "ai-automation", "Secret123!")

    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "StrongPass123!"}
    )
    headers = {"Authorization": f"Bearer {login.get_json()['access_token']}"}

    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=AI_VLAN_ACTION)
    batch = client.post(
        "/api/ai/chat", headers=headers, json={"message": "Tao VLAN 25"}
    ).get_json()["batch"]
    change_id = batch["changes"][0]["id"]
    client.post(f"/api/change-batches/{batch['id']}/approve", headers=headers)
    client.post(f"/api/change-batches/{batch['id']}/apply", headers=headers)

    surfaces = [
        client.get("/api/devices", headers=headers),
        client.get("/api/audit-logs", headers=headers),
        client.get("/api/commands/history", headers=headers),
        client.get(f"/api/changes/{change_id}", headers=headers),
        client.get(f"/api/change-batches/{batch['id']}", headers=headers),
    ]
    for response in surfaces:
        text = response.get_data(as_text=True)
        assert "Secret123!" not in text
        assert "StrongPass123!" not in text


def test_write_all_preview_confirm_and_partial_result(
    client, admin_headers, app, access_switch, dist_switch, ssh_factory
):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=WRITE_ALL_ACTION)
    ssh_factory.set_failing(access_switch.hostname, SSHConnectionError("offline"))
    ssh_factory.set_client(
        dist_switch.hostname,
        responses={
            "show running-config": "hostname DIST-SW1",
            "show startup-config": "hostname DIST-SW1",
        },
    )

    preview = client.post(
        "/api/ai/chat",
        headers=admin_headers,
        json={"message": "thuc hien lenh write tren toan bo thiet bi"},
    )
    assert preview.status_code == 200
    batch = preview.get_json()["batch"]
    assert batch["confirmation_text"] == "CONFIRM ALL"

    assert client.post(
        f"/api/change-batches/{batch['id']}/approve", headers=admin_headers
    ).status_code == 200
    applied = client.post(
        f"/api/change-batches/{batch['id']}/apply",
        headers=admin_headers,
        json={"confirmation": "CONFIRM ALL"},
    )
    assert applied.status_code == 200
    assert applied.get_json()["status"] == "partial_success"


def test_verification_failure_stops_the_flow_at_failed(
    client, app, admin_user, access_switch, ssh_factory
):
    """A device that silently ignores the change must never report success."""
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": RUNNING_CONFIG,
            # The VLAN never appears: verification must fail.
            "show vlan brief": VLAN_BEFORE,
        },
    )
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "StrongPass123!"}
    )
    headers = {"Authorization": f"Bearer {login.get_json()['access_token']}"}

    change_id = client.post(
        "/api/changes/preview",
        headers=headers,
        json={
            "device_id": access_switch.id,
            "commands": ["configure terminal", "vlan 25", "name MARKETING", "end"],
            "verification_commands": ["show vlan brief"],
        },
    ).get_json()["id"]
    client.post(f"/api/changes/{change_id}/approve", headers=headers)
    result = client.post(f"/api/changes/{change_id}/apply", headers=headers).get_json()

    assert result["status"] == "failed"
    assert result["rollback_commands"]
    failure = (
        db.session.query(AuditLog)
        .filter_by(action="change.apply", result="failure")
        .one()
    )
    assert failure is not None
