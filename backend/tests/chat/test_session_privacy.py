"""Chat sessions are private to the user who started them, even inside a
shared project: a Viewer and an Operator never see or write into each
other's conversations, and only an Operator with edit rights may push an AI
suggestion to devices."""

import pytest

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


def test_members_only_list_their_own_sessions(client, team):
    op_session = client.post("/api/chat/sessions", headers=team["operator"]).json["id"]
    viewer_session = client.post("/api/chat/sessions", headers=team["viewer"]).json["id"]

    assert [s["id"] for s in client.get("/api/chat/sessions", headers=team["operator"]).json["items"]] == [op_session]
    assert [s["id"] for s in client.get("/api/chat/sessions", headers=team["viewer"]).json["items"]] == [viewer_session]


def test_viewer_without_a_session_does_not_land_in_the_operators_chat(client, team):
    op_session = client.post("/api/chat/sessions", headers=team["operator"]).json["id"]
    assert _chat(client, team["operator"], "operator secret").status_code == 200

    # No session yet: the viewer gets a fresh one, not the operator's latest.
    assert client.get("/api/chat/messages", headers=team["viewer"]).json["items"] == []
    response = _chat(client, team["viewer"], "viewer question")
    assert response.status_code == 200
    assert response.json["session_id"] != op_session

    op_contents = [m["content"] for m in client.get(
        "/api/chat/messages", headers=team["operator"], query_string={"session_id": op_session}
    ).json["items"]]
    assert "viewer question" not in op_contents


def test_viewer_cannot_read_or_post_into_the_operators_session(client, team, db):
    op_session = client.post("/api/chat/sessions", headers=team["operator"]).json["id"]
    _chat(client, team["operator"], "operator secret", op_session)

    read = client.get("/api/chat/messages", headers=team["viewer"],
                      query_string={"session_id": op_session}).json["items"]
    assert all(m["content"] != "operator secret" for m in read)

    response = _chat(client, team["viewer"], "sneaky", op_session)
    assert response.status_code == 200
    assert response.json["session_id"] != op_session
    sneaky = db.session.query(ChatMessage).filter_by(content="sneaky").one()
    assert sneaky.session_id != op_session


def test_admin_can_audit_but_not_post_into_a_members_session(client, team, db):
    op_session = client.post("/api/chat/sessions", headers=team["operator"]).json["id"]
    _chat(client, team["operator"], "operator secret", op_session)

    read = client.get("/api/chat/messages", headers=team["admin"],
                      query_string={"session_id": op_session}).json["items"]
    assert "operator secret" in [m["content"] for m in read]
    assert client.get("/api/chat/sessions", headers=team["admin"]).json["items"] == []

    response = _chat(client, team["admin"], "admin note", op_session)
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


def test_operator_cannot_act_on_another_members_suggestion(client, team, db):
    viewer_id = db.session.query(User).filter_by(username="team_viewer").one().id
    message = _assistant_message(db, team["project"], viewer_id)
    apply = client.post(f"/api/chat/messages/{message.id}/apply-configurations",
                        headers=team["operator"], json={"approved": True})
    assert apply.status_code == 404
    export = client.post(f"/api/chat/messages/{message.id}/config-files",
                         headers=team["operator"], json={"approved": True})
    assert export.status_code == 404


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
