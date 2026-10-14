"""Audit core hardening; use the pre-collection RAM/offline runner.

The local guards supplement that runner; they do not replace its protection
against disk database connections or networking during collection/app setup.
"""
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from network_copilot.audit import service as audit_service
from network_copilot.audit.model import AuditLog
from network_copilot.audit.service import redact_sensitive


@pytest.fixture(autouse=True)
def prohibit_device_operations(monkeypatch):
    from network_copilot.ssh import client as ssh
    from network_copilot.changes import service, batch_service, verification
    from network_copilot.commands import service as commands
    from network_copilot.backups import service as backups

    def forbidden(*args, **kwargs):
        raise AssertionError("Audit hardening must never call SSH, execute commands, Apply or Verify")

    for target, name in ((ssh, "build_client_for_device"), (ssh.SSHClient, "_connect"),
                         (service, "apply"), (batch_service, "apply_batch"),
                         (commands, "execute_readonly"), (backups, "capture_backup"),
                         (verification, "run_verification")):
        monkeypatch.setattr(target, name, forbidden)


@pytest.fixture(autouse=True)
def confirm_ram_only(request, prohibit_device_operations):
    # Keep pure redaction cases free of app/database fixtures.
    if "app" not in request.fixturenames:
        return
    app = request.getfixturevalue("app")
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    assert app.config["MONITORING_ENABLED"] is False
    from network_copilot.extensions import db
    with app.app_context():
        assert db.engine.url.get_backend_name() == "sqlite"
        assert db.engine.url.database == ":memory:"


def preview(client, headers, device, description="first"):
    response = client.post("/api/changes/preview", headers=headers, json={
        "device_id": device.id, "commands": ["interface Gi0/2", "description " + description],
        "description": description,
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()


@pytest.mark.parametrize("template", ["enable secret 0 {}", "username admin password 7 {}",
                                    'password "{} with spaces"', "snmp-server community {} RO",
                                    "crypto key-string {}", "pre-shared-key {}", "api_key={}"])
def test_core_redaction_masks_encoded_quoted_and_vendor_secrets(template):
    fake = "FakeCredential_DoNotUse_123"
    assert fake not in json.dumps(redact_sensitive({"message": template.format(fake)}))
    assert fake not in json.dumps(redact_sensitive({"commands": ["vendor-unknown-syntax " + fake]}))


def test_core_preview_audit_masks_unknown_commands(client, admin_headers, access_switch, db):
    fake = "FakeCommandCredential_NotForUse123"
    created = preview(client, admin_headers, access_switch, fake)
    events = db.session.query(AuditLog).filter_by(action="change.preview").all()
    assert events
    assert all(fake not in json.dumps(event.to_dict()) for event in events)
    assert "description " + fake in client.get(f"/api/changes/{created['id']}", headers=admin_headers).get_json()["commands"]


@pytest.fixture
def fake_audit_session(monkeypatch):
    """Replace the module's DB reference; no app or database fixture is needed."""
    session = Mock(spec=["add", "commit", "rollback"])
    monkeypatch.setattr(audit_service, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(audit_service, "AuditLog", lambda **fields: SimpleNamespace(**fields))
    monkeypatch.setattr(audit_service, "_request_context", lambda: {})
    return session


def assert_safe_failure_log(caplog, *fake_secrets):
    records = [record for record in caplog.records if record.name == audit_service.logger.name]
    assert records, "An audit write failure should produce a safe diagnostic"
    assert any("RuntimeError" in record.getMessage() for record in records)
    assert all(record.exc_info is None and not record.exc_text for record in records)
    assert all(fake not in caplog.text for fake in fake_secrets)


@pytest.mark.parametrize("stage", ["add", "commit"])
def test_audit_write_failure_logs_no_secret(fake_audit_session, caplog, stage):
    fake = "FakeAuditWriteCredential_NotForUse123"
    getattr(fake_audit_session, stage).side_effect = RuntimeError(
        "INSERT audit_log failed; SQL parameters: password " + fake
    )
    with caplog.at_level(logging.ERROR, logger=audit_service.logger.name):
        result = audit_service.record_event(
            action="change.preview", result="success", message="password " + fake,
            details={"password": fake, "commands": ["vendor-unknown-syntax " + fake]},
        )

    assert result is None
    fake_audit_session.add.assert_called_once()
    assert fake_audit_session.commit.call_count == (0 if stage == "add" else 1)
    fake_audit_session.rollback.assert_called_once_with()
    attempted_event = fake_audit_session.add.call_args.args[0]
    assert fake not in json.dumps(vars(attempted_event))
    assert_safe_failure_log(caplog, fake)
    assert "SQL parameters" not in caplog.text


def test_audit_rollback_failure_remains_fail_soft(fake_audit_session, caplog):
    write_fake = "FakeAuditCommitCredential_NotForUse123"
    rollback_fake = "FakeAuditRollbackCredential_NotForUse123"
    fake_audit_session.commit.side_effect = RuntimeError("password " + write_fake)
    fake_audit_session.rollback.side_effect = RuntimeError("token " + rollback_fake)

    with caplog.at_level(logging.ERROR, logger=audit_service.logger.name):
        result = audit_service.record_event(action="audit.mock", result="failure")

    assert result is None
    fake_audit_session.add.assert_called_once()
    fake_audit_session.commit.assert_called_once_with()
    fake_audit_session.rollback.assert_called_once_with()
    assert_safe_failure_log(caplog, write_fake, rollback_fake)


def test_audit_logger_failure_does_not_escape(fake_audit_session, monkeypatch, caplog):
    write_fake = "FakeAuditWriteCredential_NotForUse123"
    logger_fake = "FakeLoggerCredential_NotForUse123"
    fake_audit_session.commit.side_effect = RuntimeError("password " + write_fake)
    failing_log = Mock(side_effect=RuntimeError("token " + logger_fake))
    monkeypatch.setattr(audit_service.logger, "error", failing_log)

    # Capture only whether an exception escaped, so a failing assertion never
    # prints the injected exception payload. The fail-soft requirement remains.
    escaped = False
    result = None
    try:
        result = audit_service.record_event(action="audit.mock", result="failure")
    except Exception:
        escaped = True

    fake_audit_session.commit.assert_called_once_with()
    failing_log.assert_called_once()
    assert write_fake not in repr(failing_log.call_args)
    assert logger_fake not in repr(failing_log.call_args)
    assert write_fake not in caplog.text and logger_fake not in caplog.text
    assert {
        "exception_escaped": escaped,
        "rollback_calls": fake_audit_session.rollback.call_count,
    } == {"exception_escaped": False, "rollback_calls": 1}
    assert result is None
