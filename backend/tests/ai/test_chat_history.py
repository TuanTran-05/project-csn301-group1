from fakes.fake_ai_provider import FakeAIProvider

from network_copilot.chat.model import ChatMessage
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


def test_chat_endpoint_records_a_blocked_command_as_a_system_message(
    client, admin_headers, app, access_switch, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=WRITE_ERASE_ACTION)

    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "write erase"}
    )

    assert response.status_code == 403
    rows = db.session.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [row.role for row in rows] == ["user", "system"]
    assert rows[1].payload["error"] == "policy_violation"
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
