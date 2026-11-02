"""An Operator who owns a project, or holds editor access to someone else's, runs
the same commands on its devices as an Admin. Anything an Admin is refused, they
are refused too; the commands' safety checks are not about the role."""

import pytest

from conftest import _create_device
from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.credentials.model import DeviceCredential
from network_copilot.projects.model import Project, ProjectMember


def _user(db, username, role):
    user = User(username=username, role=role)
    user.set_password("ParityPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


@pytest.fixture(params=["ADMIN", "OWNER", "EDITOR"])
def actor(request, app, db, ssh_factory):
    owner = _user(db, "lab_owner", "OPERATOR")
    project = Project(name="Parity Lab", management_network="10.30.0.0/24",
                      environment="pnetlab", owner_id=owner.id, ai_management_enabled=True)
    db.session.add(project)
    db.session.flush()
    if request.param == "ADMIN":
        user = _user(db, "lab_admin", "ADMIN")
    elif request.param == "OWNER":
        user = owner
    else:
        user = _user(db, "lab_editor", "OPERATOR")
        db.session.add(ProjectMember(project_id=project.id, user_id=user.id, access="editor"))
    db.session.commit()
    device = _create_device("R1", "10.30.0.1", "core", project=project)
    db.session.add(DeviceCredential(device_id=device.id, username="u", password_encrypted="p"))
    db.session.commit()
    return {
        "device": device,
        "project": project,
        "fake": ssh_factory.set_client(
            "R1", responses={"show running-config": "hostname R1", "show startup-config": "hostname R1"},
            default_output="ok",
        ),
        "headers": {"Authorization": "Bearer " + issue_token(user), "X-Project-Id": str(project.id)},
    }


def _preview(client, actor, commands=("vlan 50", "name TEST"), mode=None):
    body = {"device_id": actor["device"].id, "commands": list(commands)}
    if mode:
        body["execution_mode"] = mode
    return client.post("/api/changes/preview", headers=actor["headers"], json=body)


def _deploy(client, actor, change_id, **body):
    approved = client.post(f"/api/changes/{change_id}/approve", headers=actor["headers"])
    assert approved.status_code == 200, approved.get_json()
    return client.post(f"/api/changes/{change_id}/apply", headers=actor["headers"], json=body)


@pytest.mark.parametrize("command", [
    "show version", "show run", "ping 10.30.0.2", "tracert 10.30.0.2",
])
def test_read_only_commands(client, actor, command):
    response = client.post("/api/commands/execute-readonly", headers=actor["headers"],
                           json={"device_id": actor["device"].id, "command": command})
    assert response.status_code == 200, response.get_json()


@pytest.mark.parametrize("environment", ["virtual", "pnetlab", "production"])
def test_configuration_commands_reach_the_device(client, db, actor, environment):
    actor["device"].environment = environment
    db.session.commit()
    baseline = client.post(f"/api/devices/{actor['device'].id}/baseline", headers=actor["headers"])
    assert baseline.status_code == 201, baseline.get_json()
    created = _preview(client, actor)
    assert created.status_code == 201, created.get_json()
    applied = _deploy(client, actor, created.get_json()["id"])
    assert applied.status_code == 200, applied.get_json()
    assert any("vlan 50" in batch for batch in actor["fake"].config_batches)


@pytest.mark.parametrize("commands,mode", [
    (["write memory"], "exec"),
    (["clear ip ospf process"], "exec"),
    (["interface Gi0/1", "shutdown"], None),
])
def test_exec_and_flagged_commands(client, actor, commands, mode):
    created = _preview(client, actor, commands, mode)
    assert created.status_code == 201, created.get_json()
    applied = _deploy(client, actor, created.get_json()["id"], confirm_hostname="R1")
    assert applied.status_code == 200, applied.get_json()


def test_commands_blocked_for_an_admin_stay_blocked(client, actor):
    response = client.post("/api/commands/execute-readonly", headers=actor["headers"],
                           json={"device_id": actor["device"].id, "command": "reload"})
    assert response.status_code != 200


def test_change_through_chat_then_batch_apply(app, client, actor):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider({
        "intent": "configure",
        "operations": [{
            "device_hostnames": ["R1"], "execution_mode": "config",
            "commands": ["vlan 60"], "verification_commands": [],
        }],
        "explanation": "ok",
    })
    chat = client.post("/api/ai/chat", headers=actor["headers"], json={"message": "tạo vlan 60 trên R1"})
    assert chat.status_code == 200, chat.get_json()
    batch_id = chat.get_json()["batch"]["id"]
    approved = client.post(f"/api/change-batches/{batch_id}/approve", headers=actor["headers"])
    assert approved.status_code == 200, approved.get_json()
    applied = client.post(f"/api/change-batches/{batch_id}/apply", headers=actor["headers"],
                          json={"confirm_hostnames": ["R1"]})
    assert applied.status_code == 200, applied.get_json()


def test_ai_suggestion_apply(client, db, actor):
    from network_copilot.chat.model import ChatMessage, ChatSession

    session = ChatSession(project_id=actor["project"].id, created_by_id=None)
    db.session.add(session)
    db.session.flush()
    actor["device"].environment = "pnetlab"
    message = ChatMessage(session_id=session.id, role="assistant", content="cfg", payload={
        "configurations": [{"hostname": "R1", "device_type": "cisco_ios", "commands": ["hostname R1"]}],
    })
    db.session.add(message)
    db.session.commit()
    response = client.post(f"/api/chat/messages/{message.id}/apply-configurations",
                           headers=actor["headers"], json={"approved": True})
    assert response.status_code in (200, 201), response.get_json()


def test_read_only_operator_is_not_included(client, db, actor):
    outsider = _user(db, "lab_reader", "OPERATOR")
    db.session.add(ProjectMember(project_id=actor["project"].id, user_id=outsider.id, access="viewer"))
    db.session.commit()
    headers = {**actor["headers"], "Authorization": "Bearer " + issue_token(outsider)}
    response = client.post("/api/changes/preview", headers=headers,
                           json={"device_id": actor["device"].id, "commands": ["vlan 50"]})
    assert response.status_code == 403
