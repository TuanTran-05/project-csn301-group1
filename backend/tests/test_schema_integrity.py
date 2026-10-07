"""The database itself refuses invalid data, not just the application."""

import pytest
from sqlalchemy.exc import IntegrityError

from network_copilot.app import create_app
from network_copilot.audit.model import AuditLog
from network_copilot.auth.model import User
from network_copilot.backups.model import ConfigBackup
from network_copilot.chat.model import ChatMessage, ChatSession
from network_copilot.config import ForeignKeyTestConfig
from network_copilot.credentials.model import DeviceCredential
from network_copilot.devices.model import Device
from network_copilot.extensions import db as _db
from network_copilot.projects.model import Project, ProjectMember
from network_copilot.topology.model import TopologyLink


@pytest.fixture
def fk_app():
    application = create_app(ForeignKeyTestConfig)
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


def _project(owner=None, name="P"):
    project = Project(name=name, management_network="10.0.0.0/24", owner_id=owner.id if owner else None)
    _db.session.add(project)
    _db.session.commit()
    return project


def _user(name="u", role="OPERATOR"):
    user = User(username=name, role=role)
    user.set_password("x" * 12)
    _db.session.add(user)
    _db.session.commit()
    return user


def _device(project, host="R1", ip="10.0.0.1"):
    device = Device(project_id=project.id, hostname=host, management_ip=ip,
                    device_type="cisco_ios", role="core", ssh_port=22, status="unknown")
    _db.session.add(device)
    _db.session.commit()
    return device


# -- CHECK constraints (active in every configuration) --------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "wizard"},
        {"device_type": "juniper"},
        {"status": "great"},
        {"environment": "cloud"},
        {"ssh_port": 0},
        {"ssh_port": 70000},
    ],
)
def test_device_enums_and_port_are_checked_by_the_database(app, project, changes):
    values = dict(project_id=project.id, hostname="R1", management_ip="10.0.0.1",
                  device_type="cisco_ios", role="core", ssh_port=22, status="unknown")
    values.update(changes)
    _db.session.add(Device(**values))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_user_role_is_checked(app):
    _db.session.add(User(username="x", password_hash="h", role="ROOT"))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_project_environment_and_member_access_are_checked(app):
    _db.session.add(Project(name="bad", management_network="10.0.0.0/24", environment="moon"))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()

    project, user = _project(), _user()
    _db.session.add(ProjectMember(project_id=project.id, user_id=user.id, access="god"))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_audit_result_and_chat_role_are_checked(app, project):
    _db.session.add(AuditLog(action="x", result="maybe"))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()

    session = ChatSession(project_id=project.id)
    _db.session.add(session)
    _db.session.commit()
    _db.session.add(ChatMessage(session_id=session.id, role="robot", content="x"))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_topology_link_rules(app, project):
    a, b = _device(project), _device(project, "R2", "10.0.0.2")

    def link(**extra):
        values = dict(project_id=project.id, device_a_id=a.id, interface_a="Gi0/1",
                      device_b_id=b.id, interface_b="Gi0/1", link_type="physical")
        values.update(extra)
        return TopologyLink(**values)

    for bad in (link(device_b_id=a.id), link(link_type="wireless")):
        _db.session.add(bad)
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()

    _db.session.add(link())
    _db.session.commit()
    _db.session.add(link(interface_b="Gi0/2"))  # Gi0/1 on device A is already cabled
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_hostname_and_ip_are_unique_per_project_only(app):
    p1, p2 = _project(name="one"), _project(name="two")
    _device(p1)
    _device(p2)  # same hostname and IP in another project is fine
    _db.session.add(Device(project_id=p1.id, hostname="R1", management_ip="10.0.0.9",
                           device_type="cisco_ios", role="core", ssh_port=22, status="unknown"))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_email_is_unique_but_optional(app):
    _db.session.add_all([User(username="a", password_hash="h", role="VIEWER"),
                         User(username="b", password_hash="h", role="VIEWER")])
    _db.session.commit()  # two accounts without an email are fine
    _db.session.add_all([User(username="c", password_hash="h", role="VIEWER", email="e@x.io"),
                         User(username="d", password_hash="h", role="VIEWER", email="e@x.io")])
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


# -- foreign keys really are enforced when enabled -----------------------------


def test_foreign_keys_reject_a_dangling_reference(fk_app):
    _db.session.add(ChatSession(project_id=999))
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_deleting_a_project_cascades_through_the_database(fk_app):
    owner = _user()
    project = _project(owner)
    device = _device(project)
    _db.session.add_all([
        DeviceCredential(device_id=device.id, username="u", password_encrypted="p"),
        ConfigBackup(device_id=device.id, running_config="cfg"),
        ChatSession(project_id=project.id, created_by_id=owner.id),
    ])
    _db.session.commit()

    _db.session.execute(_db.text("DELETE FROM projects WHERE id = :id"), {"id": project.id})
    _db.session.commit()

    for model in (Device, DeviceCredential, ConfigBackup, ChatSession):
        assert _db.session.query(model).count() == 0, model.__name__


def test_deleting_a_user_keeps_history_but_clears_the_reference(fk_app):
    user = _user()
    project = _project(user)
    session = ChatSession(project_id=project.id, created_by_id=user.id)
    _db.session.add(session)
    _db.session.commit()
    _db.session.add(ChatMessage(session_id=session.id, user_id=user.id, username="u", role="user", content="hi"))
    _db.session.add(AuditLog(action="x", result="success", user_id=user.id))
    _db.session.commit()

    _db.session.execute(_db.text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    _db.session.commit()
    _db.session.expire_all()

    assert _db.session.query(ChatMessage).one().user_id is None
    assert _db.session.query(AuditLog).one().user_id is None
    assert _db.session.get(Project, project.id).owner_id is None


def test_purge_devices_still_works_with_enforcement_on(fk_app):
    from network_copilot.devices.service import purge_devices

    project = _project()
    device = _device(project)
    _db.session.add(DeviceCredential(device_id=device.id, username="u", password_encrypted="p"))
    _db.session.commit()
    purge_devices([device.id])
    assert _db.session.query(Device).count() == 0
    assert _db.session.query(DeviceCredential).count() == 0
