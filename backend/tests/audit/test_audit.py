import pytest

from network_copilot.audit.model import AuditLog
from network_copilot.audit.service import record_event, redact_sensitive
from network_copilot.changes import service as change_service
from network_copilot.extensions import db
from network_copilot.ssh.exceptions import SSHConnectionError

VLAN_COMMANDS = ["configure terminal", "vlan 25", "name MARKETING", "end"]


# -- redaction ------------------------------------------------------------


def test_redaction_removes_password():
    value = redact_sensitive({
        "username": "ai-automation",
        "password": "Secret123!",
    })
    assert value["password"] == "***REDACTED***"
    assert "Secret123!" not in str(value)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "PASSWORD",
        "secret",
        "enable_secret",
        "api_key",
        "token",
        "access_token",
        "authorization",
        "credential",
        "password_hash",
        "private_key",
    ],
)
def test_every_sensitive_key_is_redacted(key):
    assert redact_sensitive({key: "Secret123!"})[key] == "***REDACTED***"


def test_redaction_is_recursive():
    value = redact_sensitive(
        {"device": {"credentials": {"password": "Secret123!"}}, "safe": "keep"}
    )
    assert value["device"]["credentials"]["password"] == "***REDACTED***"
    assert value["safe"] == "keep"
    assert "Secret123!" not in str(value)


def test_redaction_walks_lists():
    value = redact_sensitive([{"password": "Secret123!"}, {"host": "10.10.10.11"}])
    assert value[0]["password"] == "***REDACTED***"
    assert value[1]["host"] == "10.10.10.11"


def test_redaction_masks_inline_secrets_in_strings():
    value = redact_sensitive("username ai-automation password Secret123!")
    assert "Secret123!" not in value


def test_redaction_leaves_safe_values_untouched():
    assert redact_sensitive({"hostname": "CORE-SW1"}) == {"hostname": "CORE-SW1"}
    assert redact_sensitive(None) is None
    assert redact_sensitive(42) == 42


# -- record_event ---------------------------------------------------------


def test_record_event_persists_a_log(app, admin_user, device):
    event = record_event(
        action="device.create",
        result="success",
        user_id=admin_user.id,
        device_id=device.id,
        details={"hostname": "CORE-SW1"},
    )
    assert event.id is not None
    assert db.session.query(AuditLog).count() == 1
    assert event.action == "device.create"


def test_record_event_redacts_details(app, admin_user, device):
    event = record_event(
        action="device.credential.store",
        result="success",
        user_id=admin_user.id,
        device_id=device.id,
        details={"username": "ai-automation", "password": "Secret123!"},
    )
    assert event.details["password"] == "***REDACTED***"
    assert "Secret123!" not in str(event.to_dict())


def test_record_event_never_raises(app):
    # A failure to audit must not break the request it is auditing.
    assert record_event(action="weird", result="success", details=object()) is not None


# -- events emitted by the application ------------------------------------


def _actions(app):
    return [row.action for row in db.session.query(AuditLog).all()]


def test_login_success_is_audited(client, admin_user, app):
    client.post(
        "/api/auth/login", json={"username": "admin", "password": "StrongPass123!"}
    )
    log = db.session.query(AuditLog).filter_by(action="auth.login").one()
    assert log.result == "success"
    assert log.user_id == admin_user.id


def test_failed_login_is_audited_without_the_password(client, admin_user, app):
    client.post(
        "/api/auth/login", json={"username": "admin", "password": "hunter2-wrong"}
    )
    log = db.session.query(AuditLog).filter_by(action="auth.login").one()
    assert log.result == "failure"
    assert "hunter2-wrong" not in str(log.to_dict())


def test_device_create_update_delete_are_audited(client, admin_headers, app):
    created = client.post(
        "/api/devices",
        headers=admin_headers,
        json={
            "hostname": "CORE-SW1",
            "management_ip": "10.10.10.11",
            "device_type": "cisco_ios",
            "role": "core",
        },
    ).get_json()
    client.put(
        f"/api/devices/{created['id']}", headers=admin_headers, json={"role": "access"}
    )
    client.delete(f"/api/devices/{created['id']}", headers=admin_headers)

    actions = _actions(app)
    assert "device.create" in actions
    assert "device.update" in actions
    assert "device.delete" in actions


def test_ssh_test_connection_is_audited(client, admin_headers, device, ssh_factory, app):
    ssh_factory.set_client(device.hostname, reachable=True)
    client.post(f"/api/devices/{device.id}/test-connection", headers=admin_headers)
    log = db.session.query(AuditLog).filter_by(action="device.test_connection").one()
    assert log.result == "success"


def test_failed_ssh_test_connection_is_audited(
    client, admin_headers, device, ssh_factory, app
):
    ssh_factory.set_failing(device.hostname, SSHConnectionError("no route"))
    client.post(f"/api/devices/{device.id}/test-connection", headers=admin_headers)
    log = db.session.query(AuditLog).filter_by(action="device.test_connection").one()
    assert log.result == "failure"


def test_readonly_command_is_audited(client, admin_headers, device, ssh_factory, app):
    ssh_factory.set_client(device.hostname, default_output="ok")
    client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )
    log = db.session.query(AuditLog).filter_by(action="command.readonly").one()
    assert log.result == "success"
    assert log.details["command"] == "show ip route"


def test_blocked_command_is_audited(client, admin_headers, device, ssh_factory, app):
    ssh_factory.set_client(device.hostname)
    client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "write erase"},
    )
    log = db.session.query(AuditLog).filter_by(action="command.blocked").one()
    assert log.result == "blocked"


def test_change_lifecycle_is_audited(
    client, admin_headers, admin_user, access_switch, ssh_factory, app
):
    ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show vlan brief": (
                "VLAN Name                             Status    Ports\n"
                "---- -------------------------------- --------- ------\n"
                "25   MARKETING                        active\n"
            ),
        },
    )
    change_id = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={
            "device_id": access_switch.id,
            "commands": VLAN_COMMANDS,
            "verification_commands": ["show vlan brief"],
        },
    ).get_json()["id"]
    client.post(f"/api/changes/{change_id}/approve", headers=admin_headers)
    client.post(f"/api/changes/{change_id}/apply", headers=admin_headers)

    actions = _actions(app)
    assert "change.preview" in actions
    assert "change.approve" in actions
    assert "change.apply" in actions
    applied = db.session.query(AuditLog).filter_by(action="change.apply").one()
    assert applied.result == "success"


def test_failed_apply_is_audited_as_failure(
    client, admin_headers, access_switch, ssh_factory, app
):
    ssh_factory.set_failing(access_switch.hostname, SSHConnectionError("down"))
    change_id = client.post(
        "/api/changes/preview",
        headers=admin_headers,
        json={"device_id": access_switch.id, "commands": VLAN_COMMANDS},
    ).get_json()["id"]
    client.post(f"/api/changes/{change_id}/approve", headers=admin_headers)
    client.post(f"/api/changes/{change_id}/apply", headers=admin_headers)

    log = db.session.query(AuditLog).filter_by(action="change.apply").one()
    assert log.result == "failure"


# -- API ------------------------------------------------------------------


def test_audit_log_endpoint_requires_admin(client, viewer_headers):
    assert client.get("/api/audit-logs", headers=viewer_headers).status_code == 403


def test_audit_log_endpoint_requires_authentication(client):
    assert client.get("/api/audit-logs").status_code == 401


def test_audit_log_endpoint_lists_events(client, admin_headers, app):
    response = client.get("/api/audit-logs", headers=admin_headers)
    assert response.status_code == 200
    assert any(item["action"] == "auth.login" for item in response.get_json()["items"])


def test_audit_log_filters(client, admin_headers, admin_user, device, ssh_factory, app):
    ssh_factory.set_client(device.hostname, default_output="ok")
    client.post(
        "/api/commands/execute-readonly",
        headers=admin_headers,
        json={"device_id": device.id, "command": "show ip route"},
    )

    by_action = client.get(
        "/api/audit-logs?action=command.readonly", headers=admin_headers
    ).get_json()["items"]
    assert len(by_action) == 1

    by_device = client.get(
        f"/api/audit-logs?device_id={device.id}", headers=admin_headers
    ).get_json()["items"]
    assert all(item["device_id"] == device.id for item in by_device)

    by_user = client.get(
        f"/api/audit-logs?user_id={admin_user.id}", headers=admin_headers
    ).get_json()["items"]
    assert len(by_user) >= 1

    by_result = client.get(
        "/api/audit-logs?result=success", headers=admin_headers
    ).get_json()["items"]
    assert all(item["result"] == "success" for item in by_result)


def test_audit_log_time_filters(client, admin_headers, app):
    future = client.get(
        "/api/audit-logs?since=2999-01-01T00:00:00", headers=admin_headers
    )
    assert future.status_code == 200
    assert future.get_json()["items"] == []

    past = client.get(
        "/api/audit-logs?since=2000-01-01T00:00:00&until=2999-01-01T00:00:00",
        headers=admin_headers,
    )
    assert len(past.get_json()["items"]) >= 1


def test_audit_log_rejects_a_bad_timestamp(client, admin_headers):
    response = client.get("/api/audit-logs?since=not-a-date", headers=admin_headers)
    assert response.status_code == 422


def test_audit_response_never_contains_credentials(
    client, admin_headers, admin_user, device, app
):
    record_event(
        action="device.credential.store",
        result="success",
        user_id=admin_user.id,
        device_id=device.id,
        details={"username": "ai-automation", "password": "Secret123!"},
    )
    body = client.get("/api/audit-logs", headers=admin_headers).get_data(as_text=True)
    assert "Secret123!" not in body
