"""The model-backed intent router, replayed on g1's real conversation.

The fake provider scripts each answer, so these tests check the routing: which
pipeline each message reaches, what the router is told, and that a failed
classification falls back to the keyword rules.
"""

import json

import pytest
from fakes.fake_ai_provider import FakeAIProvider

from conftest import _create_device

ROUTER = "You route one chat message"
ADVISOR = "patient network design tutor"
OPERATIONS = "network operations assistant"

DIAGRAM = {
    "title": "1 router, 1 switch, 6 PC",
    "nodes": [{"id": "r1", "label": "ROUTER-R1", "kind": "router"},
              {"id": "sw1", "label": "SWITCH-SW1", "kind": "switch"}],
    "links": [{"source": "r1", "target": "sw1", "label": "LAN"}],
}
DESIGN = {"explanation": "Đề xuất sơ đồ.", "diagram": DIAGRAM}
CHAT = {"intent": "chat", "operations": [], "explanation": "Xin chào!"}


@pytest.fixture
def operator(client, app, db):
    from network_copilot.auth.model import User
    from network_copilot.projects.model import Project

    app.config["AI_INTENT_ROUTER"] = True
    user = User(username="g1-operator", role="OPERATOR")
    user.set_password("StrongPass123!")
    db.session.add(user)
    db.session.flush()
    project = Project(name="labdemo", owner_id=user.id, management_network="172.16.3.0/24",
                      environment="pnetlab")
    db.session.add(project)
    db.session.commit()
    _create_device("ROUTER-R1", "172.16.3.14", "core", project=project)
    _create_device("SWITCH-SW1", "172.16.3.17", "access", project=project)
    token = client.post("/api/auth/login", json={
        "username": "g1-operator", "password": "StrongPass123!"}).get_json()["access_token"]
    return {"Authorization": f"Bearer {token}", "X-Project-Id": str(project.id)}


class Chat:
    """One conversation: each message gets the router's label, then the pipeline's answer."""

    def __init__(self, client, app, headers):
        self.client, self.app, self.headers = client, app, headers
        self.session_id = None
        self.provider = None

    def say(self, message, *answers):
        self.provider = FakeAIProvider(responses=list(answers))
        self.app.config["AI_PROVIDER_INSTANCE"] = self.provider
        body = {"message": message}
        if self.session_id:
            body["session_id"] = self.session_id
        response = self.client.post("/api/ai/chat", headers=self.headers, json=body)
        assert response.status_code == 200, response.get_json()
        self.session_id = response.get_json()["session_id"]
        return response.get_json()

    def prompts(self):
        return [p["system_prompt"] for p in self.provider.prompts]

    def router_context(self):
        return next(p["context"] for p in self.provider.prompts if ROUTER in p["system_prompt"])


def test_g1s_conversation_reaches_the_right_pipeline_each_time(client, app, operator):
    chat = Chat(client, app, operator)

    reply = chat.say("hãy xây 1 hệ thống mạng gồm 1 router, 1 switch và 6 pc", {"intent": "design"}, DESIGN)
    assert reply["intent"] == "advice"
    assert ROUTER in chat.prompts()[0] and ADVISOR in chat.prompts()[1]
    assert chat.router_context()["last_reply_was_design"] is False

    # A follow-up with no design word: the router keeps it in the design.
    reply = chat.say("đặt dải ip là 172.20.5.0/24", {"intent": "design"}, DESIGN)
    assert reply["intent"] == "advice"
    assert chat.router_context()["last_reply_was_design"] is True
    assert chat.provider.prompts[1]["context"]["previous_diagram"]["title"] == DIAGRAM["title"]

    # A read on a real device, named by its short name: the operations pipeline.
    reply = chat.say("chạy show running config trên sw1", {"intent": "read", "devices": ["SWITCH-SW1"]}, CHAT)
    assert OPERATIONS in chat.prompts()[1]
    assert reply["intent"] == "chat"

    # A plain CLI line needs no classification at all.
    chat.say("show ip interface brief", CHAT)
    assert len(chat.provider.prompts) == 1 and OPERATIONS in chat.prompts()[0]

    # A greeting in the middle of it all.
    chat.say("cảm ơn bạn", {"intent": "chat"}, CHAT)
    assert OPERATIONS in chat.prompts()[1]


def test_the_router_gets_device_names_and_recent_turns_but_no_addresses(client, app, operator):
    chat = Chat(client, app, operator)
    chat.say("xin chào", {"intent": "chat"}, CHAT)
    chat.say("vẽ sơ đồ mạng cho tôi", {"intent": "design"}, DESIGN)
    context = chat.router_context()
    assert context["devices"] == [{"hostname": "ROUTER-R1", "kind": "router"},
                                  {"hostname": "SWITCH-SW1", "kind": "switch"}]
    assert [turn["role"] for turn in context["conversation"]] == ["user", "assistant"]
    assert "172.16.3." not in json.dumps(context)  # management IPs never reach the model


@pytest.mark.parametrize("router_answer", ["not json at all", {"intent": "launch-missiles"}])
def test_an_unusable_classification_falls_back_to_the_keyword_rules(client, app, operator, router_answer):
    chat = Chat(client, app, operator)
    chat.say("thiết kế mạng gồm 1 router và 1 switch", {"intent": "design"}, DESIGN)
    # The keyword rules see a follow-up of the design just suggested.
    reply = chat.say("thêm 1 switch nữa", router_answer, DESIGN)
    assert reply["intent"] == "advice"


def test_a_failing_router_call_falls_back_to_the_keyword_rules(client, app, operator):
    from network_copilot.ai import intent

    chat = Chat(client, app, operator)
    calls = []

    def broken(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("quota")

    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=DESIGN)
    original = intent._provider
    intent._provider = lambda: type("P", (), {"complete": staticmethod(broken)})()
    try:
        reply = chat.say("thiết kế mạng gồm 1 router và 1 switch", DESIGN)
    finally:
        intent._provider = original
    assert calls and reply["intent"] == "advice"


def test_the_router_cannot_unlock_design_for_a_project_viewer(client, app, db, operator, viewer_headers):
    """Read-only members never reach the advisor, whatever a model would say."""
    from network_copilot.auth.model import User
    from network_copilot.projects.model import ProjectMember

    project_id = int(operator["X-Project-Id"])
    viewer = db.session.query(User).filter_by(username="viewer").one()
    db.session.add(ProjectMember(project_id=project_id, user_id=viewer.id, access="viewer"))
    db.session.commit()
    chat = Chat(client, app, {**viewer_headers, "X-Project-Id": str(project_id)})
    chat.say("kiểm tra trạng thái sw1", CHAT)
    assert len(chat.provider.prompts) == 1 and OPERATIONS in chat.prompts()[0]
