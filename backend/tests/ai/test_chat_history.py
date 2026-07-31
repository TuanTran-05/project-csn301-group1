import pytest
from flask_jwt_extended import create_access_token

from fakes.fake_ai_provider import FakeAIProvider

from network_copilot.ai.service import AIService
from network_copilot.app import create_app
from network_copilot.auth.model import User
from network_copilot.chat.model import ChatMessage
from network_copilot.config import TestConfig
from network_copilot.extensions import db

MONITOR_ACTION = {
    "intent": "monitor",
    "device_hostname": "DIST-SW1",
    "commands": ["show ip route"],
    "verification_commands": [],
    "explanation": "Checking the routing table.",
}

WRITE_ERASE_ACTION = {
    "intent": "configure",
    "device_hostname": "ACC-SW1",
    "commands": ["write erase"],
    "verification_commands": [],
    "explanation": "Wiping the configuration.",
}


class ChatRateLimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True


@pytest.fixture
def rate_limited_app():
    application = create_app(ChatRateLimitedConfig)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def test_chat_endpoint_records_a_user_and_assistant_message(
    client, admin_headers, app, dist_switch, ssh_factory
):
    ssh_factory.set_client(dist_switch.hostname, default_output="ok")
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=MONITOR_ACTION)

    client.post("/api/ai/chat", headers=admin_headers, json={"message": "check routes"})

    rows = db.session.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[0].content == "check routes"
    assert rows[1].content == "Checking the routing table."


def test_chat_endpoint_records_a_dangerous_proposal_as_an_assistant_message(
    client, admin_headers, app, access_switch, ssh_factory
):
    """The AI has full authority to propose a dangerous command: it still
    only creates a Preview (never touches SSH), recorded as a normal
    assistant turn, just flagged for extra confirmation before Apply."""
    fake = ssh_factory.set_client(access_switch.hostname)
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=WRITE_ERASE_ACTION)

    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "write erase"}
    )

    assert response.status_code == 200
    assert response.get_json()["change"]["requires_confirmation"] is True
    rows = db.session.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].payload["change"]["requires_confirmation"] is True
    assert fake.calls == []


def test_chat_endpoint_response_shape_is_unchanged(
    client, admin_headers, app, dist_switch, ssh_factory
):
    ssh_factory.set_client(dist_switch.hostname, default_output="ok")
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=MONITOR_ACTION)

    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "check routes"}
    )
    body = response.get_json()
    assert body["intent"] == "monitor"
    assert body["explanation"] == "Checking the routing table."


def test_chat_endpoint_records_a_validation_failure_once(client, admin_headers):
    response = client.post("/api/ai/chat", headers=admin_headers, json={})

    assert response.status_code == 422
    assert response.get_json()["error"] == "validation_error"
    rows = db.session.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [row.role for row in rows] == ["system"]
    assert rows[0].payload["error"] == "validation_error"


def test_chat_endpoint_records_a_rate_limit_failure_once(rate_limited_app):
    with rate_limited_app.app_context():
        user = User(username="admin", role="ADMIN")
        user.set_password("StrongPass123!")
        db.session.add(user)
        db.session.commit()
        token = create_access_token(
            identity=str(user.id),
            additional_claims={"role": user.role, "username": user.username},
        )

    client = rate_limited_app.test_client()
    headers = {"Authorization": f"Bearer {token}"}
    responses = [
        client.post("/api/ai/chat", headers=headers, json={}) for _ in range(21)
    ]

    assert [response.status_code for response in responses] == [422] * 20 + [429]
    rows = db.session.query(ChatMessage).order_by(ChatMessage.id).all()
    assert len(rows) == 21
    assert [row.role for row in rows] == ["system"] * 21
    assert rows[-1].payload["error"] == "rate_limit_exceeded"


def test_chat_endpoint_records_an_unexpected_failure_once(
    client, admin_headers, monkeypatch
):
    def boom(self, message, user_id):
        raise RuntimeError("provider secret")

    monkeypatch.setattr(AIService, "handle", boom)

    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "check routes"}
    )

    assert response.status_code == 500
    assert response.get_json()["error"] == "internal_error"
    rows = db.session.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [row.role for row in rows] == ["user", "system"]
    assert rows[1].content == "An internal error occurred."
    assert rows[1].payload["error"] == "internal_error"


def test_chat_endpoint_does_not_record_an_unauthenticated_attempt(client):
    response = client.post("/api/ai/chat", json={"message": "check routes"})

    assert response.status_code == 401
    assert db.session.query(ChatMessage).count() == 0


def test_chat_endpoint_records_failure_for_valid_jwt_with_deleted_user(client, app):
    with app.app_context():
        token = create_access_token(
            identity="999999",
            additional_claims={"role": "ADMIN", "username": "deleted-admin"},
        )

    response = client.post(
        "/api/ai/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert response.status_code == 422
    rows = db.session.query(ChatMessage).order_by(ChatMessage.id).all()
    assert len(rows) == 1
    assert rows[0].role == "system"
    assert rows[0].user_id is None
    assert rows[0].username is None
    assert rows[0].payload["error"] == "validation_error"
