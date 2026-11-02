"""Chat sessions are shared by the members of a project, and only someone with
edit rights may act on an AI suggestion. The default lab is the exception: any
account can open it, so its sessions stay private to whoever started them."""

import pytest

from conftest import default_project_id
from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.chat.model import ChatMessage, ChatSession
from network_copilot.projects.model import Project, ProjectMember


def _user(db, username, role):
    user = User(username=username, role=role)
    user.set_password("PrivacyPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _headers(user, project):
    return {"Authorization": "Bearer " + issue_token(user), "X-Project-Id": str(project.id)}


@pytest.fixture
def team(app, db):
    operator = _user(db, "op_owner", "OPERATOR")
    viewer = _user(db, "team_viewer", "VIEWER")
    admin = _user(db, "team_admin", "ADMIN")
    project = Project(name="Team Lab", management_network="10.20.0.0/24",
                      environment="pnetlab", owner_id=operator.id,
                      ai_management_enabled=True)
    db.session.add(project)
    db.session.flush()
    db.session.add(ProjectMember(project_id=project.id, user_id=viewer.id, access="viewer"))
    db.session.commit()
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider({
        "intent": "chat", "operations": [], "explanation": "ok",
    })
    return {
        "project": project,
        "operator": _headers(operator, project),
        "viewer": _headers(viewer, project),
        "admin": _headers(admin, project),
    }


def _chat(client, headers, message, session_id=None):
    body = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    return client.post("/api/ai/chat", headers=headers, json=body)


def test_members_list_every_session_of_the_project(client, team):
    op_session = client.post("/api/chat/sessions", headers=team["operator"]).json["id"]
    viewer_session = client.post("/api/chat/sessions", headers=team["viewer"]).json["id"]

    for headers in (team["operator"], team["viewer"]):
        listed = {s["id"] for s in client.get("/api/chat/sessions", headers=headers).json["items"]}
        assert listed == {op_session, viewer_session}


def test_viewer_without_a_session_joins_the_projects_latest_chat(client, team):
    op_session = client.post("/api/chat/sessions", headers=team["operator"]).json["id"]
    assert _chat(client, team["operator"], "operator question").status_code == 200

    contents = [m["content"] for m in client.get("/api/chat/messages", headers=team["viewer"]).json["items"]]
    assert "operator question" in contents
    response = _chat(client, team["viewer"], "viewer question")
    assert response.status_code == 200
    assert response.json["session_id"] == op_session


def test_viewer_can_read_and_continue_an_operators_session(client, team, db):
    op_session = client.post("/api/chat/sessions", headers=team["operator"]).json["id"]
    _chat(client, team["operator"], "operator question", op_session)

    read = client.get("/api/chat/messages", headers=team["viewer"],
                      query_string={"session_id": op_session}).json["items"]
    assert "operator question" in [m["content"] for m in read]

    response = _chat(client, team["viewer"], "viewer follow-up", op_session)
    assert response.status_code == 200
    assert response.json["session_id"] == op_session
    assert db.session.query(ChatMessage).filter_by(content="viewer follow-up").one().session_id == op_session


def test_a_non_member_cannot_reach_a_projects_session(client, team, db):
    op_session = client.post("/api/chat/sessions", headers=team["operator"]).json["id"]
    outsider = _user(db, "outsider", "OPERATOR")
    db.session.commit()
    read = client.get("/api/chat/messages", headers=_headers(outsider, team["project"]),
                      query_string={"session_id": op_session})
    assert read.status_code == 404


def _action(intent, command):
    return FakeAIProvider({
        "intent": intent,
        "operations": [{
            "device_hostnames": ["R1"],
            "execution_mode": "config" if intent == "configure" else "exec",
            "commands": [command],
            "verification_commands": [],
        }],
        "explanation": "ok",
    })


def _team_device(db, team, ssh_factory):
    from conftest import _create_device
    from network_copilot.credentials.model import DeviceCredential

    device = _create_device("R1", "10.20.0.1", "core", project=team["project"])
    db.session.add(DeviceCredential(device_id=device.id, username="u", password_encrypted="p"))
    db.session.commit()
    ssh_factory.set_client("R1", default_output="ok")


def test_viewer_can_run_a_read_only_command_through_chat(app, client, team, db, ssh_factory):
    _team_device(db, team, ssh_factory)
    app.config["AI_PROVIDER_INSTANCE"] = _action("monitor", "show version")
    response = _chat(client, team["viewer"], "kiểm tra R1")
    assert response.status_code == 200, response.get_json()
    assert response.json["intent"] == "monitor"


def test_viewer_cannot_configure_through_chat(app, client, team, db, ssh_factory):
    _team_device(db, team, ssh_factory)
    app.config["AI_PROVIDER_INSTANCE"] = _action("configure", "vlan 25")
    assert _chat(client, team["viewer"], "tạo vlan 25").status_code == 403


def test_editor_operator_can_preview_a_change_through_chat(app, client, team, db, ssh_factory):
    _team_device(db, team, ssh_factory)
    app.config["AI_PROVIDER_INSTANCE"] = _action("configure", "vlan 25")
    response = _chat(client, team["operator"], "tạo vlan 25")
    assert response.status_code == 200, response.get_json()
    assert response.json["intent"] == "configure"


@pytest.fixture
def lab(app, db):
    """Three accounts nobody added to anything: the default lab is open to all."""
    project = db.session.get(Project, default_project_id())
    operator = _user(db, "lab_operator", "OPERATOR")
    viewer = _user(db, "lab_viewer", "VIEWER")
    admin = _user(db, "lab_admin", "ADMIN")
    db.session.commit()
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider({
        "intent": "chat", "operations": [], "explanation": "ok",
    })
    return {
        "operator": _headers(operator, project),
        "viewer": _headers(viewer, project),
        "admin": _headers(admin, project),
    }


def test_default_lab_members_only_list_their_own_sessions(client, lab):
    op_session = client.post("/api/chat/sessions", headers=lab["operator"]).json["id"]
    viewer_session = client.post("/api/chat/sessions", headers=lab["viewer"]).json["id"]

    assert [s["id"] for s in client.get("/api/chat/sessions", headers=lab["operator"]).json["items"]] == [op_session]
    assert [s["id"] for s in client.get("/api/chat/sessions", headers=lab["viewer"]).json["items"]] == [viewer_session]


def test_default_lab_viewer_without_a_session_gets_a_fresh_one(client, lab):
    op_session = client.post("/api/chat/sessions", headers=lab["operator"]).json["id"]
    assert _chat(client, lab["operator"], "operator question").status_code == 200

    assert client.get("/api/chat/messages", headers=lab["viewer"]).json["items"] == []
    response = _chat(client, lab["viewer"], "viewer question")
    assert response.status_code == 200
    assert response.json["session_id"] != op_session


def test_default_lab_viewer_cannot_read_or_post_into_an_operators_session(client, lab, db):
    op_session = client.post("/api/chat/sessions", headers=lab["operator"]).json["id"]
    _chat(client, lab["operator"], "operator question", op_session)

    read = client.get("/api/chat/messages", headers=lab["viewer"],
                      query_string={"session_id": op_session}).json["items"]
    assert all(m["content"] != "operator question" for m in read)

    response = _chat(client, lab["viewer"], "sneaky", op_session)
    assert response.status_code == 200
    assert response.json["session_id"] != op_session
    assert db.session.query(ChatMessage).filter_by(content="sneaky").one().session_id != op_session


def test_default_lab_admin_can_audit_but_not_post_into_a_session(client, lab):
    op_session = client.post("/api/chat/sessions", headers=lab["operator"]).json["id"]
    _chat(client, lab["operator"], "operator question", op_session)

    read = client.get("/api/chat/messages", headers=lab["admin"],
                      query_string={"session_id": op_session}).json["items"]
    assert "operator question" in [m["content"] for m in read]
    assert client.get("/api/chat/sessions", headers=lab["admin"]).json["items"] == []

    response = _chat(client, lab["admin"], "admin note", op_session)
    assert response.status_code == 200
    assert response.json["session_id"] != op_session


def _assistant_message(db, project, created_by):
    session = ChatSession(project_id=project.id, created_by_id=created_by)
    db.session.add(session)
    db.session.flush()
    message = ChatMessage(session_id=session.id, role="assistant", content="cfg",
                          payload={"configurations": [{"hostname": "R1", "device_type": "cisco_ios",
                                                       "commands": ["hostname R1"]}]})
    db.session.add(message)
    db.session.commit()
    return message


def test_viewer_cannot_apply_ai_configurations(client, team, db):
    viewer_id = db.session.query(User).filter_by(username="team_viewer").one().id
    message = _assistant_message(db, team["project"], viewer_id)
    response = client.post(f"/api/chat/messages/{message.id}/apply-configurations",
                           headers=team["viewer"], json={"approved": True})
    assert response.status_code == 403


def test_only_an_editor_can_export_a_suggestion_from_a_shared_session(client, team, db):
    viewer_id = db.session.query(User).filter_by(username="team_viewer").one().id
    message = _assistant_message(db, team["project"], viewer_id)
    export = client.post(f"/api/chat/messages/{message.id}/config-files",
                         headers=team["operator"], json={"approved": True})
    assert export.status_code == 200
    export = client.post(f"/api/chat/messages/{message.id}/config-files",
                         headers=team["viewer"], json={"approved": True})
    assert export.status_code == 403


def test_operator_can_approve_their_own_ai_suggestion(client, team, db, ssh_factory):
    """The AI Management consent check runs on this path; it used to raise a
    NameError and answer 500."""
    from conftest import _create_device
    from network_copilot.credentials.model import DeviceCredential

    device = _create_device("R1", "10.20.0.1", "core", project=team["project"])
    device.environment = "pnetlab"
    db.session.add(DeviceCredential(device_id=device.id, username="u", password_encrypted="p"))
    db.session.commit()
    ssh_factory.set_client("R1", default_output="hostname R1")
    operator_id = db.session.query(User).filter_by(username="op_owner").one().id
    message = _assistant_message(db, team["project"], operator_id)

    response = client.post(f"/api/chat/messages/{message.id}/apply-configurations",
                           headers=team["operator"], json={"approved": True})

    assert response.status_code in (200, 201), response.get_json()
    batch = response.get_json()["batch"]
    assert batch["chat_message_id"] == message.id
    listed = client.get("/api/change-batches", headers=team["operator"]).get_json()["items"]
    assert batch["id"] in [item["id"] for item in listed]
