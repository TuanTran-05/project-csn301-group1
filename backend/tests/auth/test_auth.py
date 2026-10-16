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


def test_login_token_survives_nonzero_account_version(client, viewer_user, db):
    viewer_user.token_version = 7
    db.session.commit()
    response = client.post("/api/auth/login", json={
        "username": "viewer", "password": "ViewerPass123!",
    })
    assert response.status_code == 200
    headers = {"Authorization": "Bearer " + response.json["access_token"]}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.get("/api/projects", headers=headers).status_code == 200

    viewer_user.token_version += 1
    db.session.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_admin_sessions_coexist_and_password_change_revokes_both(app, admin_user):
    clients = [app.test_client(), app.test_client()]
    tokens = []
    for client in clients:
        response = client.post("/api/auth/login", json={"username": "admin", "password": "StrongPass123!"})
        assert response.status_code == 200
        tokens.append({"Authorization": "Bearer " + response.json["access_token"]})
    assert tokens[0] != tokens[1]
    for client, token in zip(clients, tokens):
        assert client.get("/api/users", headers=token).status_code == 200
    changed = clients[0].post("/api/auth/change-password", headers=tokens[0], json={
        "current_password": "StrongPass123!", "new_password": "NewStrongPass456!"})
    assert changed.status_code == 200
    for client, token in zip(clients, tokens):
        assert client.get("/api/users", headers=token).status_code == 401
    fresh = {"Authorization": "Bearer " + changed.json["access_token"]}
    assert clients[0].get("/api/users", headers=fresh).status_code == 200
    login = clients[1].post("/api/auth/login", json={"username": "admin", "password": "NewStrongPass456!"})
    assert login.status_code == 200
    assert clients[1].get("/api/users", headers={"Authorization": "Bearer " + login.json["access_token"]}).status_code == 200
