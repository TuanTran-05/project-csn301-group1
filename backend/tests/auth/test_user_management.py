import pytest

from conftest import ADMIN_PASSWORD, _auth_headers, _create_user
from network_copilot.auth.model import User
from network_copilot.extensions import db

NEW_PASSWORD = "A-brand-new-pass-1"


def make(client, headers, **extra):
    payload = {"username": "dave", "password": "A-long-password-1", "role": "OPERATOR", **extra}
    return client.post("/api/users", headers=headers, json=payload)


def test_admin_creates_a_user_with_profile_fields(client, admin_headers, admin_user):
    response = make(client, admin_headers, full_name="Dave D", email="Dave@Example.com")
    assert response.status_code == 201
    body = response.get_json()
    assert body["full_name"] == "Dave D"
    assert body["email"] == "dave@example.com"  # normalised
    assert "password" not in str(body)
    assert db.session.get(User, body["id"]).created_by_id == admin_user.id


def test_duplicate_username_or_email_conflicts(client, admin_headers):
    assert make(client, admin_headers, email="d@example.com").status_code == 201
    assert make(client, admin_headers, email="other@example.com").status_code == 409
    assert make(client, admin_headers, username="erin", email="d@example.com").status_code == 409


@pytest.mark.parametrize(
    "extra",
    [{"password": "short"}, {"role": "ROOT"}, {"username": "a b"}, {"email": "nope"}],
)
def test_invalid_user_payloads_are_rejected(client, admin_headers, extra):
    assert make(client, admin_headers, **extra).status_code == 422


def test_only_admins_manage_users(client, viewer_headers, viewer_user, admin_user):
    assert client.get("/api/users", headers=viewer_headers).status_code == 403
    assert client.put(f"/api/users/{admin_user.id}", headers=viewer_headers, json={"role": "VIEWER"}).status_code == 403
    assert client.post(f"/api/users/{admin_user.id}/reset-password", headers=viewer_headers, json={}).status_code == 403


def test_admin_updates_role_and_profile(client, admin_headers):
    user_id = make(client, admin_headers).get_json()["id"]
    response = client.put(
        f"/api/users/{user_id}", headers=admin_headers,
        json={"role": "VIEWER", "full_name": "  New Name  "},
    )
    assert response.status_code == 200
    assert (response.get_json()["role"], response.get_json()["full_name"]) == ("VIEWER", "New Name")


def test_unknown_or_invalid_fields_are_rejected(client, admin_headers):
    user_id = make(client, admin_headers).get_json()["id"]
    assert client.put(f"/api/users/{user_id}", headers=admin_headers, json={"password": "x"}).status_code == 422
    assert client.put(f"/api/users/{user_id}", headers=admin_headers, json={"is_active": "no"}).status_code == 422
    assert client.put(f"/api/users/9999", headers=admin_headers, json={"role": "VIEWER"}).status_code == 404


def test_demoting_a_user_revokes_their_existing_token(client, admin_headers):
    user_id = make(client, admin_headers, username="frank", role="ADMIN").get_json()["id"]
    frank = _auth_headers(client, "frank", "A-long-password-1")
    assert client.get("/api/users", headers=frank).status_code == 200

    client.put(f"/api/users/{user_id}", headers=admin_headers, json={"role": "VIEWER"})
    assert client.get("/api/users", headers=frank).status_code == 401  # token revoked
    fresh = _auth_headers(client, "frank", "A-long-password-1")
    assert client.get("/api/users", headers=fresh).status_code == 403  # and the role really changed


def test_role_is_read_from_the_database_not_the_token(client, admin_headers, app):
    from flask_jwt_extended import create_access_token

    user = _create_user("stale", "A-long-password-1", "VIEWER")
    with app.app_context():
        forged_role = create_access_token(
            identity=str(user.id),
            additional_claims={"role": "ADMIN", "username": "stale", "tv": 0},
        )
    response = client.get("/api/users", headers={"Authorization": f"Bearer {forged_role}"})
    assert response.status_code == 403


def test_deactivated_user_cannot_log_in_or_use_a_token(client, admin_headers):
    user_id = make(client, admin_headers, username="gina").get_json()["id"]
    token = _auth_headers(client, "gina", "A-long-password-1")
    assert client.put(f"/api/users/{user_id}", headers=admin_headers, json={"is_active": False}).status_code == 200

    assert client.get("/api/auth/me", headers=token).status_code == 401
    login = client.post("/api/auth/login", json={"username": "gina", "password": "A-long-password-1"})
    assert login.status_code == 401

    client.put(f"/api/users/{user_id}", headers=admin_headers, json={"is_active": True})
    assert client.post("/api/auth/login", json={"username": "gina", "password": "A-long-password-1"}).status_code == 200


def test_the_last_active_admin_cannot_be_removed(client, admin_headers, admin_user):
    for body in ({"role": "OPERATOR"}, {"is_active": False}):
        response = client.put(f"/api/users/{admin_user.id}", headers=admin_headers, json=body)
        assert response.status_code == 409


def test_an_admin_cannot_demote_or_disable_themselves_even_with_another_admin(client, admin_headers, admin_user):
    make(client, admin_headers, username="second", role="ADMIN")
    assert client.put(f"/api/users/{admin_user.id}", headers=admin_headers, json={"role": "VIEWER"}).status_code == 409
    assert client.put(f"/api/users/{admin_user.id}", headers=admin_headers, json={"is_active": False}).status_code == 409


def test_one_admin_can_demote_another_while_two_remain_active(client, admin_headers):
    other = make(client, admin_headers, username="second", role="ADMIN").get_json()["id"]
    assert client.put(f"/api/users/{other}", headers=admin_headers, json={"role": "OPERATOR"}).status_code == 200


def test_admin_resets_a_password_and_old_sessions_end(client, admin_headers):
    user_id = make(client, admin_headers, username="hank").get_json()["id"]
    old = _auth_headers(client, "hank", "A-long-password-1")

    assert client.post(f"/api/users/{user_id}/reset-password", headers=admin_headers,
                       json={"new_password": NEW_PASSWORD}).status_code == 204
    assert client.get("/api/auth/me", headers=old).status_code == 401
    assert client.post("/api/auth/login", json={"username": "hank", "password": "A-long-password-1"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "hank", "password": NEW_PASSWORD}).status_code == 200


def test_reset_password_enforces_the_policy_and_never_logs_it(client, admin_headers):
    from network_copilot.audit.model import AuditLog

    user_id = make(client, admin_headers, username="ivy").get_json()["id"]
    assert client.post(f"/api/users/{user_id}/reset-password", headers=admin_headers,
                       json={"new_password": "short"}).status_code == 422
    client.post(f"/api/users/{user_id}/reset-password", headers=admin_headers,
                json={"new_password": NEW_PASSWORD})
    logged = " ".join(str(row.details) for row in db.session.query(AuditLog))
    assert NEW_PASSWORD not in logged


def test_user_changes_their_own_password(client, admin_headers, admin_user):
    response = client.post(
        "/api/auth/change-password", headers=admin_headers,
        json={"current_password": ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 200
    # The old token died, the returned one works.
    assert client.get("/api/auth/me", headers=admin_headers).status_code == 401
    fresh = {"Authorization": f"Bearer {response.get_json()['access_token']}"}
    assert client.get("/api/auth/me", headers=fresh).status_code == 200
    assert client.post("/api/auth/login", json={"username": "admin", "password": NEW_PASSWORD}).status_code == 200


def test_change_password_needs_the_current_password(client, admin_headers):
    wrong = client.post("/api/auth/change-password", headers=admin_headers,
                        json={"current_password": "nope-nope-nope", "new_password": NEW_PASSWORD})
    assert wrong.status_code == 403
    same = client.post("/api/auth/change-password", headers=admin_headers,
                       json={"current_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD})
    assert same.status_code == 422


def test_login_records_the_time(client, admin_user):
    assert admin_user.last_login_at is None
    client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    db.session.refresh(admin_user)
    assert admin_user.last_login_at is not None
