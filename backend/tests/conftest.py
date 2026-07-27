import pytest

from network_copilot.app import create_app
from network_copilot.auth.model import User
from network_copilot.config import TestConfig
from network_copilot.extensions import db as _db

ADMIN_PASSWORD = "StrongPass123!"
VIEWER_PASSWORD = "ViewerPass123!"


@pytest.fixture
def app():
    application = create_app(TestConfig)
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def client(app):
    return app.test_client()


def _create_user(username: str, password: str, role: str) -> User:
    user = User(username=username, role=role)
    user.set_password(password)
    _db.session.add(user)
    _db.session.commit()
    return user


@pytest.fixture
def admin_user(app):
    return _create_user("admin", ADMIN_PASSWORD, "ADMIN")


@pytest.fixture
def viewer_user(app):
    return _create_user("viewer", VIEWER_PASSWORD, "VIEWER")


def _auth_headers(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return {"Authorization": f"Bearer {response.get_json()['access_token']}"}


@pytest.fixture
def admin_headers(client, admin_user):
    return _auth_headers(client, "admin", ADMIN_PASSWORD)


@pytest.fixture
def viewer_headers(client, viewer_user):
    return _auth_headers(client, "viewer", VIEWER_PASSWORD)
