"""Authentication, invalid inputs and device isolation using only test DB RAM."""
from datetime import timedelta

import pytest
from flask_jwt_extended import create_access_token

from network_copilot.auth.service import issue_token
from network_copilot.auth.model import User

from .test_history_routes import BASE, push


@pytest.fixture(autouse=True)
def prohibit_device_operations(monkeypatch):
    from network_copilot.ssh import client as ssh
    from network_copilot.changes import service, batch_service
    from network_copilot.commands import service as commands
    from network_copilot.backups import service as backups

    def forbidden(*args, **kwargs):
        raise AssertionError("History must never call SSH, execute commands or Apply")

    for target, name in ((ssh, "build_client_for_device"), (ssh.SSHClient, "_connect"),
                         (service, "apply"), (batch_service, "apply_batch"),
                         (commands, "execute_readonly"), (backups, "capture_backup")):
        monkeypatch.setattr(target, name, forbidden)


ENDPOINTS = [("GET", ""), ("GET", "/backup"), ("POST", "/states"),
             ("POST", "/undo"), ("POST", "/redo"), ("POST", "/restore")]


@pytest.mark.parametrize("method,suffix", ENDPOINTS)
def test_missing_invalid_expired_jwt_denied(app, client, admin_user, method, suffix):
    expired = create_access_token(identity=str(admin_user.id), expires_delta=timedelta(seconds=-5),
                                  additional_claims={"role": "ADMIN", "username": "admin"})
    for headers in ({}, {"Authorization": "Bearer invalid.token"}, {"Authorization": "Bearer " + expired}):
        assert client.open(BASE + suffix, method=method, headers=headers, json={}).status_code == 401


@pytest.mark.parametrize("method,suffix", ENDPOINTS)
def test_database_role_downgrade_overrides_stale_admin_claim(client, admin_headers, admin_user, db, method, suffix):
    admin_user.role = "VIEWER"
    db.session.commit()
    assert client.open(BASE + suffix, method=method, headers=admin_headers, json={}).status_code == 403


def test_deleted_or_unknown_admin_identity_rejected(client):
    token = create_access_token(identity="999999", additional_claims={"role": "ADMIN"})
    # An identity with no account is refused as unauthenticated.
    assert client.get(BASE, headers={"Authorization": "Bearer " + token}).status_code == 401


def test_empty_and_baseline_moves_are_conflicts_without_audit(client, admin_headers):
    for suffix in ("/undo", "/redo"):
        assert client.post(BASE + suffix, headers=admin_headers).status_code == 409
    assert client.get(BASE + "/backup", headers=admin_headers).get_json()["audit_trail"] == []
    push(client, admin_headers, {"interfaces": {}})
    for suffix in ("/undo", "/redo"):
        assert client.post(BASE + suffix, headers=admin_headers).status_code == 409
    assert len(client.get(BASE + "/backup", headers=admin_headers).get_json()["audit_trail"]) == 1


def test_preview_does_not_automatically_create_history(client, admin_headers, access_switch):
    response = client.post("/api/changes/preview", headers=admin_headers, json={
        "device_id": access_switch.id, "commands": ["interface Gi0/2", "description REVIEW_ONLY"],
        "description": "Preview is not a draft history integration",
    })
    assert response.status_code == 201
    assert client.get(BASE, headers=admin_headers).get_json()["snapshot"] is None
    assert client.get(BASE + "/backup", headers=admin_headers).get_json()["audit_trail"] == []


def test_all_history_operations_remain_user_and_topology_scoped(client, admin_headers, db, second_admin_allowed):
    other = User(username="separate-admin", role="ADMIN")
    other.set_password("SeparateHistoryTestPassword!")
    db.session.add(other)
    db.session.commit()
    other_headers = {"Authorization": "Bearer " + issue_token(other)}
    a, b = "?topology_id=a", "?topology_id=b"
    push(client, admin_headers, {"description": "a0"}, a)
    push(client, admin_headers, {"description": "a1"}, a)
    push(client, admin_headers, {"description": "b0"}, b)
    assert client.post(BASE + "/undo" + a, headers=admin_headers).get_json()["snapshot"] == {"description": "a0"}
    assert client.get(BASE + b, headers=admin_headers).get_json()["snapshot"] == {"description": "b0"}
    assert client.get(BASE + "/backup" + a, headers=other_headers).get_json()["audit_trail"] == []
    assert client.post(BASE + "/redo" + a, headers=other_headers).status_code == 409
    backup = client.get(BASE + "/backup" + a, headers=admin_headers).get_json()
    # Explicit restore affects only the authenticated receiving user's target topology.
    response = client.post(BASE + "/restore" + b, headers=other_headers, json=backup)
    assert response.status_code == 200 and response.get_json()["can_redo"]
    assert client.get(BASE + b, headers=admin_headers).get_json()["snapshot"] == {"description": "b0"}
    assert client.get(BASE + a, headers=other_headers).get_json()["snapshot"] is None
    audit = client.get(BASE + "/backup" + b, headers=other_headers).get_json()["audit_trail"]
    assert audit[-1]["action_type"] == "RESTORE" and audit[-1]["actor"].startswith("actor-")


def test_invalid_inputs_and_size_limit_do_not_change_history(client, admin_headers):
    push(client, admin_headers, {"description": "keep"})
    before = client.get(BASE, headers=admin_headers).get_json()
    for payload in ({}, {"snapshot": []}, {"snapshot": {}, "action_type": "", "description": "edit"}):
        assert client.post(BASE + "/states", headers=admin_headers, json=payload).status_code == 400
    for query in ("?topology_id=", "?topology_id=%20", "?topology_id=" + "x" * 129):
        assert client.get(BASE + query, headers=admin_headers).status_code == 400
    assert client.post(BASE + "/states", headers=admin_headers, data=b"x" * (2 * 1024 * 1024 + 1)).status_code == 422
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    backup["history_state"]["redo_stack"] = [999999]
    assert client.post(BASE + "/restore", headers=admin_headers, json=backup).status_code == 400
    assert client.get(BASE, headers=admin_headers).get_json() == before


def test_restore_undo_redo_without_device_or_apply_operations(client, admin_headers):
    push(client, admin_headers, {"description": "before"})
    push(client, admin_headers, {"description": "after"})
    assert client.post(BASE + "/undo", headers=admin_headers).status_code == 200
    backup = client.get(BASE + "/backup", headers=admin_headers).get_json()
    assert client.post(BASE + "/restore", headers=admin_headers, json=backup).status_code == 200
    assert client.post(BASE + "/redo", headers=admin_headers).get_json()["snapshot"] == {"description": "after"}
