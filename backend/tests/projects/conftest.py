"""Fixtures for multi-project tests: several users, several projects."""

import pytest

from conftest import _auth_headers, _create_device, _create_user
from network_copilot.extensions import db
from network_copilot.projects.model import Project

PASSWORD = "UserPass12345!"


def make_project(owner, name, network="10.10.0.0/24", environment="pnetlab"):
    project = Project(
        name=name,
        management_network=network,
        environment=environment,
        owner_id=owner.id if owner else None,
    )
    db.session.add(project)
    db.session.commit()
    return project


@pytest.fixture
def alice(app):
    return _create_user("alice", PASSWORD, "OPERATOR")


@pytest.fixture
def bob(app):
    return _create_user("bob", PASSWORD, "OPERATOR")


@pytest.fixture
def alice_headers(client, alice):
    return _auth_headers(client, "alice", PASSWORD)


@pytest.fixture
def bob_headers(client, bob):
    return _auth_headers(client, "bob", PASSWORD)


@pytest.fixture
def two_projects(app, admin_user, alice, bob):
    """Alice owns A, Bob owns B; both have a device named R1."""
    a = make_project(alice, "Alpha", "10.10.0.0/24")
    b = make_project(bob, "Beta", "10.10.0.0/24")
    ra = _create_device("R1", "10.10.0.1", "core", project=a)
    rb = _create_device("R1", "10.10.0.1", "core", project=b)
    return a, b, ra, rb


def scope(headers, project):
    return {**headers, "X-Project-Id": str(project.id)}
