"""Read-only access to someone else's project lets a member run diagnostics, both
for a VIEWER account and for an OPERATOR who was only given viewer access."""

import pytest

from conftest import _create_device
from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.changes.model import ChangeRequest
from network_copilot.credentials.model import DeviceCredential
from network_copilot.projects.model import Project, ProjectMember

COMMANDS = [
    ("show version", "show version"),
    ("show ip route", "show ip route"),
    ("ping 10.20.0.2", "ping 10.20.0.2"),
    ("traceroute 10.20.0.2", "traceroute 10.20.0.2"),
    ("tracert 10.20.0.2", "traceroute 10.20.0.2"),
]


def _user(db, username, role):
    user = User(username=username, role=role)
    user.set_password("DiagnosticsPass123!")
    db.session.add(user)
    db.session.flush()
    return user


@pytest.fixture(params=["VIEWER", "OPERATOR"])
def member(request, db, ssh_factory):
    """A read-only member of a project owned by another Operator, with one device."""
    owner = _user(db, "project_owner", "OPERATOR")
    project = Project(name="Owner Lab", management_network="10.20.0.0/24",
                      environment="pnetlab", owner_id=owner.id)
    db.session.add(project)
    db.session.flush()
    user = _user(db, "read_only_member", request.param)
    db.session.add(ProjectMember(project_id=project.id, user_id=user.id, access="viewer"))
    db.session.commit()
    device = _create_device("R1", "10.20.0.1", "core", project=project)
    db.session.add(DeviceCredential(device_id=device.id, username="u", password_encrypted="p"))
    db.session.commit()
    return {
        "device": device,
        "fake": ssh_factory.set_client("R1", default_output="ok"),
        "headers": {"Authorization": "Bearer " + issue_token(user), "X-Project-Id": str(project.id)},
    }


def _provider(app, intent, command):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider({
        "intent": intent,
        "operations": [{
            "device_hostnames": ["R1"],
            "execution_mode": "config" if intent == "configure" else "exec",
            "commands": [command],
            "verification_commands": [],
        }],
        "explanation": "ok",
    })


@pytest.mark.parametrize("command,sent", COMMANDS)
def test_member_runs_diagnostics_through_the_api(client, member, command, sent):
    response = client.post("/api/commands/execute-readonly", headers=member["headers"],
                           json={"device_id": member["device"].id, "command": command})
    assert response.status_code == 200, response.get_json()
    assert member["fake"].show_commands == [sent]


@pytest.mark.parametrize("command,sent", COMMANDS)
def test_member_runs_diagnostics_through_chat(app, client, member, command, sent):
    _provider(app, "monitor", command)
    response = client.post("/api/ai/chat", headers=member["headers"],
                           json={"message": f"{command} trên R1"})
    assert response.status_code == 200, response.get_json()
    assert member["fake"].show_commands == [sent]


@pytest.mark.parametrize("message", [
    "kiểm tra topology bằng show cdp neighbors trên R1",
    "xem sơ đồ rồi ping 10.20.0.2 từ R1",
])
def test_mentioning_the_topology_does_not_block_a_diagnostic(app, client, member, message):
    _provider(app, "monitor", "show ip route")
    response = client.post("/api/ai/chat", headers=member["headers"], json={"message": message})
    assert response.status_code == 200, response.get_json()
    assert member["fake"].show_commands == ["show ip route"]


@pytest.mark.parametrize("command", ["show run", "show running-config", "SHOW  RUN"])
def test_member_runs_show_run_through_the_api(client, member, command):
    response = client.post("/api/commands/execute-readonly", headers=member["headers"],
                           json={"device_id": member["device"].id, "command": command})
    assert response.status_code == 200, response.get_json()
    assert member["fake"].show_commands == ["show running-config"]


@pytest.mark.parametrize("message", ["show run", "show run R1", "show run trên r1", "show running-config R1"])
def test_member_runs_show_run_through_chat_without_the_model(app, client, member, message):
    _provider(app, "chat", "unused")
    provider = app.config["AI_PROVIDER_INSTANCE"]
    member["fake"].default_output = "hostname R1\nenable secret 5 abc123\ninterface Gi0/0"
    response = client.post("/api/ai/chat", headers=member["headers"], json={"message": message})
    assert response.status_code == 200, response.get_json()
    assert response.json["intent"] == "monitor"
    assert member["fake"].show_commands == ["show running-config"]
    output = response.json["results"][0]["output"]
    assert "hostname R1" in output and "abc123" not in output
    assert provider.calls == 0


def test_config_never_reaches_the_model_in_a_later_turn(app, client, member):
    member["fake"].default_output = "hostname R1\ninterface Gi0/77"
    first = client.post("/api/ai/chat", headers=member["headers"], json={"message": "show run"})
    assert first.status_code == 200, first.get_json()
    provider = FakeAIProvider({"intent": "chat", "operations": [], "explanation": "ok"})
    app.config["AI_PROVIDER_INSTANCE"] = provider
    next_turn =client.post("/api/ai/chat", headers=member["headers"], json={"message": "chào bạn"})
    assert next_turn.status_code == 200, next_turn.get_json()
    assert "Gi0/77" not in provider.everything_sent()


def test_show_run_asks_which_device_when_it_is_ambiguous(client, db, member):
    project = db.session.get(Project, member["device"].project_id)
    second = _create_device("R2", "10.20.0.2", "core", project=project)
    db.session.add(DeviceCredential(device_id=second.id, username="u", password_encrypted="p"))
    db.session.commit()
    response = client.post("/api/ai/chat", headers=member["headers"], json={"message": "show run"})
    assert response.status_code == 200
    assert response.json["intent"] == "chat"
    assert member["fake"].show_commands == []


def test_the_model_still_cannot_request_a_running_config(app, client, member):
    _provider(app, "monitor", "show run")
    response = client.post("/api/ai/chat", headers=member["headers"], json={"message": "xem toàn bộ cấu hình R1"})
    assert response.status_code != 200
    assert member["fake"].show_commands == []


@pytest.mark.parametrize("command", ["configure terminal", "reload", "write erase", "ping"])
def test_member_cannot_run_anything_else_through_the_api(client, member, command):
    response = client.post("/api/commands/execute-readonly", headers=member["headers"],
                           json={"device_id": member["device"].id, "command": command})
    assert response.status_code != 200
    assert member["fake"].show_commands == []


def test_member_cannot_configure_through_chat(app, client, db, member):
    _provider(app, "configure", "vlan 25")
    response = client.post("/api/ai/chat", headers=member["headers"], json={"message": "tạo vlan 25 trên R1"})
    assert response.status_code == 403
    assert db.session.query(ChangeRequest).count() == 0


def test_member_cannot_create_a_project_through_chat(app, client, member):
    _provider(app, "monitor", "show version")
    response = client.post("/api/ai/chat", headers=member["headers"], json={"message": "tạo project mới"})
    assert response.status_code == 403
