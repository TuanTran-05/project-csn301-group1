from network_copilot.auth.model import User


def test_login_returns_token(client, admin_user):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "StrongPass123!"},
    )
    assert response.status_code == 200
    assert response.get_json()["user"]["role"] == "ADMIN"


def test_login_rejects_wrong_password(client, admin_user):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_login_returns_access_token(client, admin_user):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "StrongPass123!"},
    )
    assert response.get_json()["access_token"]


def test_password_is_hashed(app):
    user = User(username="hash-check", role="VIEWER")
    user.set_password("StrongPass123!")
    assert user.password_hash != "StrongPass123!"
    assert "StrongPass123!" not in user.password_hash
    assert user.check_password("StrongPass123!") is True
    assert user.check_password("nope") is False


def test_me_returns_current_user(client, admin_headers):
    response = client.get("/api/auth/me", headers=admin_headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body["username"] == "admin"
    assert body["role"] == "ADMIN"
    assert "password_hash" not in body


def test_me_requires_token(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 401


def test_login_rejects_unknown_user(client, admin_user):
    response = client.post(
        "/api/auth/login",
        json={"username": "ghost", "password": "StrongPass123!"},
    )
    assert response.status_code == 401


def test_login_requires_credentials(client):
    response = client.post("/api/auth/login", json={})
    assert response.status_code == 400
