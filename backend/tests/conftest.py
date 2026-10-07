import pytest

from network_copilot.app import create_app
from network_copilot.auth.model import User
from network_copilot.config import TestConfig
from network_copilot.devices.model import Device
from network_copilot.extensions import db as _db
from network_copilot.projects.model import Project, ProjectMember

from fakes.fake_ssh_client import FakeSSHClient

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


def _default_project() -> Project:
    """The one project the single-project tests live in (admins see it too)."""
    project = _db.session.query(Project).filter_by(name="Default Lab").first()
    if project is None:
        project = Project(
            name="Default Lab",
            management_network="172.16.3.0/24",
            environment="pnetlab",
        )
        _db.session.add(project)
        _db.session.commit()
    return project


def default_project_id() -> int:
    return _default_project().id


@pytest.fixture
def project(app):
    return _default_project()


def _create_user(username: str, password: str, role: str) -> User:
    user = User(username=username, role=role)
    user.set_password(password)
    _db.session.add(user)
    _db.session.commit()
    return user


def _share_default_project(user: User, access: str = "viewer") -> None:
    _db.session.add(
        ProjectMember(project_id=_default_project().id, user_id=user.id, access=access)
    )
    _db.session.commit()


@pytest.fixture
def admin_user(app):
    return _create_user("admin", ADMIN_PASSWORD, "ADMIN")


@pytest.fixture
def viewer_user(app):
    user = _create_user("viewer", VIEWER_PASSWORD, "VIEWER")
    _share_default_project(user)
    return user


class SSHFactoryStub:
    """Registry of FakeSSHClient instances keyed by device hostname.

    Installed as the app's SSH_CLIENT_FACTORY so no test ever opens a socket.
    """

    def __init__(self):
        self.clients: dict[str, FakeSSHClient] = {}
        self.default: FakeSSHClient | None = None

    def __call__(self, device):
        client = self.clients.get(device.hostname, self.default)
        if client is None:
            client = FakeSSHClient()
            self.clients[device.hostname] = client
        return client

    def set_client(self, hostname: str, **kwargs) -> FakeSSHClient:
        client = FakeSSHClient(**kwargs)
        self.clients[hostname] = client
        return client

    def set_failing(self, hostname: str, error: Exception) -> FakeSSHClient:
        return self.set_client(hostname, fail_with=error)

    def get(self, hostname: str) -> FakeSSHClient:
        return self.clients[hostname]


@pytest.fixture
def ssh_factory(app):
    factory = SSHFactoryStub()
    app.config["SSH_CLIENT_FACTORY"] = factory
    return factory


def _auth_headers(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return {"Authorization": f"Bearer {response.get_json()['access_token']}"}


def _create_device(
    hostname: str, ip: str, role: str, device_type="cisco_ios", project=None
) -> Device:
    device = Device(
        project_id=(project or _default_project()).id,
        hostname=hostname,
        management_ip=ip,
        device_type=device_type,
        role=role,
        ssh_port=22,
        status="unknown",
        monitoring_enabled=True,
    )
    _db.session.add(device)
    _db.session.commit()
    return device


@pytest.fixture
def device(app):
    """Default lab device: the core switch."""
    return _create_device("CORE-SW1", "172.16.3.111", "core")


@pytest.fixture
def core_switch(device):
    return device


@pytest.fixture
def access_switch(app):
    return _create_device("ACC-SW1", "172.16.3.131", "access")


@pytest.fixture
def dist_switch(app):
    return _create_device("DIST-SW1", "172.16.3.121", "distribution")


@pytest.fixture
def make_device(app):
    return _create_device


@pytest.fixture
def admin_headers(client, admin_user, project):
    return _auth_headers(client, "admin", ADMIN_PASSWORD)


@pytest.fixture
def viewer_headers(client, viewer_user):
    return _auth_headers(client, "viewer", VIEWER_PASSWORD)
