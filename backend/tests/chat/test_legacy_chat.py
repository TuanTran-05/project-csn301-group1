import pytest

from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.auth.model import User
from network_copilot.auth.service import issue_token
from network_copilot.projects.model import ProjectMember


@pytest.mark.parametrize("role", ["VIEWER", "OPERATOR"])
def test_shared_project_allows_legacy_role_chat(app, db, client, project, role):
    user = User(username="legacy_chat", role=role)
    user.set_password("LegacyChatPassword123!")
    db.session.add(user)
    db.session.flush()
    db.session.add(ProjectMember(project_id=project.id, user_id=user.id, access="viewer"))
    db.session.commit()
    token = issue_token(user)
    headers = {"Authorization": "Bearer " + token, "X-Project-Id": str(project.id)}
    assert client.get("/api/projects", headers=headers).json["items"][0]["id"] == project.id
    session = client.post("/api/chat/sessions", headers=headers)
    assert session.status_code == 201
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider({
        "intent": "chat", "operations": [], "explanation": "Xin chào!",
    })
    response = client.post("/api/ai/chat", headers=headers, json={
        "message": "Xin chào", "session_id": session.json["id"],
    })
    assert response.status_code == 200
    assert response.json["intent"] == "chat"
    messages = client.get("/api/chat/messages", headers=headers, query_string={"session_id": session.json["id"]})
    assert messages.status_code == 200
    assert [message["role"] for message in messages.json["items"]] == ["user", "assistant"]
    assert client.post("/api/devices", headers=headers, json={}).status_code == 403
