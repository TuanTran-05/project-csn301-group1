import pytest
from fakes.fake_ai_provider import FakeAIProvider

from network_copilot.ai.schemas import AIAction
from network_copilot.ai.service import AIService
from network_copilot.audit.model import AuditLog
from network_copilot.changes.model import ChangeRequest
from network_copilot.commands.model import CommandExecution
from network_copilot.credentials.service import store_device_credential
from network_copilot.errors import PolicyViolationError, ValidationError
from network_copilot.extensions import db

MONITOR_ACTION = {
    "intent": "monitor",
    "device_hostname": "DIST-SW1",
    "commands": ["show ip ospf neighbor"],
    "verification_commands": [],
    "explanation": "Checking OSPF adjacencies on DIST-SW1.",
}

CONFIGURE_ACTION = {
    "intent": "configure",
    "device_hostname": "ACC-SW1",
    "commands": ["configure terminal", "vlan 25", "name MARKETING", "end"],
    "verification_commands": ["show vlan brief"],
    "explanation": "Creating VLAN 25 named MARKETING on ACC-SW1.",
}

DANGEROUS_ACTION = {
    "intent": "configure",
    "device_hostname": "ACC-SW1",
    "commands": ["write erase"],
    "verification_commands": [],
    "explanation": "Wiping the configuration.",
}

OSPF_OUTPUT = """Neighbor ID     Pri   State           Dead Time   Address         Interface
2.2.2.2           1   FULL/DR         00:00:33    10.255.0.6      GigabitEthernet0/2
"""


def service_with(app, payload) -> tuple[AIService, FakeAIProvider]:
    provider = FakeAIProvider(responses=payload)
    return AIService(provider=provider), provider


# -- schema ---------------------------------------------------------------


def test_ai_action_schema_accepts_a_valid_payload():
    action = AIAction(**MONITOR_ACTION)
    assert action.intent == "monitor"
    assert action.device_hostname == "DIST-SW1"
    assert action.commands == ["show ip ospf neighbor"]


def test_ai_action_defaults_verification_commands_to_empty():
    action = AIAction(
        intent="monitor",
        device_hostname="CORE-SW1",
        commands=["show ip route"],
        explanation="Reading the routing table.",
    )
    assert action.verification_commands == []


@pytest.mark.parametrize("intent", ["reboot", "delete", "", "MONITOR"])
def test_ai_action_rejects_unknown_intents(intent):
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        AIAction(
            intent=intent,
            device_hostname="CORE-SW1",
            commands=["show ip route"],
            explanation="x",
        )


# -- interpret ------------------------------------------------------------


def test_interpret_returns_a_structured_action(app, dist_switch, admin_user):
    service, _ = service_with(app, MONITOR_ACTION)
    action = service.interpret("Kiem tra OSPF cua DIST-SW1", admin_user.id)
    assert isinstance(action, AIAction)
    assert action.intent == "monitor"


def test_interpret_rejects_malformed_model_output(app, admin_user):
    service, _ = service_with(app, "this is not json")
    with pytest.raises(ValidationError):
        service.interpret("hello", admin_user.id)


def test_interpret_rejects_an_incomplete_action(app, admin_user):
    service, _ = service_with(app, {"intent": "monitor"})
    with pytest.raises(ValidationError):
        service.interpret("hello", admin_user.id)


# -- monitor intent -------------------------------------------------------


def test_monitor_intent_runs_readonly_commands(
    app, dist_switch, admin_user, ssh_factory
):
    ssh_factory.set_client(
        dist_switch.hostname, responses={"show ip ospf neighbor": OSPF_OUTPUT}
    )
    service, _ = service_with(app, MONITOR_ACTION)
    result = service.handle("Kiem tra OSPF cua DIST-SW1", admin_user.id)

    assert result["intent"] == "monitor"
    assert "FULL/DR" in result["results"][0]["output"]
    assert db.session.query(CommandExecution).count() == 1
    assert db.session.query(ChangeRequest).count() == 0


def test_monitor_intent_returns_parsed_data(
    app, dist_switch, admin_user, ssh_factory
):
    ssh_factory.set_client(
        dist_switch.hostname, responses={"show ip ospf neighbor": OSPF_OUTPUT}
    )
    service, _ = service_with(app, MONITOR_ACTION)
    result = service.handle("Check OSPF on DIST-SW1", admin_user.id)
    assert result["results"][0]["parsed"][0]["state"] == "FULL/DR"


def test_monitor_intent_never_opens_a_config_session(
    app, dist_switch, admin_user, ssh_factory
):
    fake = ssh_factory.set_client(
        dist_switch.hostname, responses={"show ip ospf neighbor": OSPF_OUTPUT}
    )
    service, _ = service_with(app, MONITOR_ACTION)
    service.handle("Check OSPF on DIST-SW1", admin_user.id)
    assert fake.config_batches == []


# -- configure intent -----------------------------------------------------


def test_configure_intent_only_creates_a_preview(
    app, access_switch, admin_user, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    service, _ = service_with(app, CONFIGURE_ACTION)
    result = service.handle("Tao VLAN 25 MARKETING tren ACC-SW1", admin_user.id)

    assert result["intent"] == "configure"
    assert result["change"]["status"] == "pending_approval"
    # No SSH at all: preview is a pure planning step.
    assert fake.calls == []
    assert fake.config_batches == []


def test_configure_intent_persists_the_change_request(
    app, access_switch, admin_user, ssh_factory
):
    service, _ = service_with(app, CONFIGURE_ACTION)
    service.handle("Tao VLAN 25 MARKETING tren ACC-SW1", admin_user.id)
    change = db.session.query(ChangeRequest).one()
    assert change.status == "pending_approval"
    assert change.source == "ai"
    assert change.commands == CONFIGURE_ACTION["commands"]


def test_configure_intent_requires_approval_before_anything_runs(
    app, access_switch, admin_user
):
    service, _ = service_with(app, CONFIGURE_ACTION)
    result = service.handle("Tao VLAN 25", admin_user.id)
    assert result["requires_approval"] is True


# -- dangerous commands ---------------------------------------------------


def test_write_erase_from_ai_is_blocked(app, access_switch, admin_user, ssh_factory):
    fake = ssh_factory.set_client(access_switch.hostname)
    service, _ = service_with(app, DANGEROUS_ACTION)

    with pytest.raises(PolicyViolationError):
        service.handle("Xoa cau hinh ACC-SW1", admin_user.id)

    assert fake.calls == []
    assert db.session.query(ChangeRequest).count() == 0


def test_blocked_ai_command_is_audited(app, access_switch, admin_user, ssh_factory):
    ssh_factory.set_client(access_switch.hostname)
    service, _ = service_with(app, DANGEROUS_ACTION)
    with pytest.raises(PolicyViolationError):
        service.handle("Xoa cau hinh ACC-SW1", admin_user.id)

    log = db.session.query(AuditLog).filter_by(action="ai.command_blocked").one()
    assert log.result == "blocked"


def test_monitor_intent_with_a_dangerous_command_is_blocked(
    app, access_switch, admin_user, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    service, _ = service_with(
        app,
        {
            "intent": "monitor",
            "device_hostname": "ACC-SW1",
            "commands": ["reload"],
            "verification_commands": [],
            "explanation": "Restarting.",
        },
    )
    with pytest.raises(PolicyViolationError):
        service.handle("reload ACC-SW1", admin_user.id)
    assert fake.show_commands == []


def test_unknown_device_from_ai_is_rejected(app, admin_user):
    from network_copilot.errors import NotFoundError

    service, _ = service_with(
        app,
        {
            "intent": "monitor",
            "device_hostname": "GHOST-SW",
            "commands": ["show ip route"],
            "verification_commands": [],
            "explanation": "x",
        },
    )
    with pytest.raises(NotFoundError):
        service.handle("check GHOST-SW", admin_user.id)


# -- prompt hygiene -------------------------------------------------------


def test_context_contains_only_safe_device_metadata(
    app, dist_switch, admin_user, ssh_factory
):
    store_device_credential(dist_switch.id, "ai-automation", "Secret123!")
    ssh_factory.set_client(dist_switch.hostname, default_output="ok")
    service, provider = service_with(app, MONITOR_ACTION)
    service.handle("Check OSPF on DIST-SW1", admin_user.id)

    context = provider.prompts[0]["context"]
    device_entry = next(
        item for item in context["devices"] if item["hostname"] == "DIST-SW1"
    )
    assert set(device_entry) == {"hostname", "role", "device_type", "status"}
    assert "supported_commands" in context
    assert "supported_actions" in context


def test_no_credentials_are_ever_sent_to_the_model(
    app, dist_switch, admin_user, ssh_factory
):
    store_device_credential(dist_switch.id, "ai-automation", "Secret123!")
    ssh_factory.set_client(dist_switch.hostname, default_output="ok")
    service, provider = service_with(app, MONITOR_ACTION)
    service.handle("Check OSPF on DIST-SW1", admin_user.id)

    sent = provider.everything_sent()
    assert "Secret123!" not in sent
    assert "ai-automation" not in sent
    assert "password" not in sent.lower()


def test_management_ip_is_not_sent_to_the_model(
    app, dist_switch, admin_user, ssh_factory
):
    ssh_factory.set_client(dist_switch.hostname, default_output="ok")
    service, provider = service_with(app, MONITOR_ACTION)
    service.handle("Check OSPF on DIST-SW1", admin_user.id)
    assert "10.10.10.21" not in provider.everything_sent()


def test_full_running_config_is_never_sent_to_the_model(
    app, dist_switch, admin_user, ssh_factory
):
    ssh_factory.set_client(dist_switch.hostname, default_output="ok")
    service, provider = service_with(app, MONITOR_ACTION)
    service.handle("Check OSPF on DIST-SW1", admin_user.id)
    assert "show running-config" not in provider.everything_sent()


# -- troubleshooting ------------------------------------------------------


def test_troubleshoot_runs_diagnostics_then_explains(
    app, dist_switch, admin_user, ssh_factory
):
    ssh_factory.set_client(
        dist_switch.hostname, responses={"show ip ospf neighbor": OSPF_OUTPUT}
    )
    provider = FakeAIProvider(
        responses={
            "intent": "troubleshoot",
            "device_hostname": "DIST-SW1",
            "commands": ["show ip ospf neighbor"],
            "verification_commands": [],
            "explanation": "Collecting OSPF diagnostics.",
        }
    )
    service = AIService(provider=provider)
    result = service.handle("Tai sao DIST-SW1 mat OSPF?", admin_user.id)

    assert result["intent"] == "troubleshoot"
    assert result["results"][0]["command"] == "show ip ospf neighbor"
    assert result["analysis"]
    # Phase 1 proposes diagnostics, phase 2 explains the collected output.
    assert provider.calls == 2
    assert provider.prompts[1].get("mode") == "explain"


def test_troubleshoot_never_applies_a_fix(app, dist_switch, admin_user, ssh_factory):
    fake = ssh_factory.set_client(
        dist_switch.hostname, responses={"show ip ospf neighbor": OSPF_OUTPUT}
    )
    provider = FakeAIProvider(
        responses={
            "intent": "troubleshoot",
            "device_hostname": "DIST-SW1",
            "commands": ["show ip ospf neighbor"],
            "verification_commands": [],
            "explanation": "Collecting diagnostics.",
        }
    )
    AIService(provider=provider).handle("Debug DIST-SW1", admin_user.id)
    assert fake.config_batches == []
    assert db.session.query(ChangeRequest).count() == 0


# -- API ------------------------------------------------------------------


def test_chat_endpoint_requires_authentication(client):
    assert client.post("/api/ai/chat", json={"message": "hi"}).status_code == 401


def test_chat_endpoint_returns_the_monitor_result(
    client, admin_headers, app, dist_switch, ssh_factory
):
    ssh_factory.set_client(
        dist_switch.hostname, responses={"show ip ospf neighbor": OSPF_OUTPUT}
    )
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=MONITOR_ACTION)

    response = client.post(
        "/api/ai/chat",
        headers=admin_headers,
        json={"message": "Kiem tra OSPF cua DIST-SW1"},
    )
    assert response.status_code == 200
    assert response.get_json()["intent"] == "monitor"


def test_chat_endpoint_creates_a_preview_for_configure(
    client, admin_headers, app, access_switch
):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=CONFIGURE_ACTION)
    response = client.post(
        "/api/ai/chat",
        headers=admin_headers,
        json={"message": "Tao VLAN 25 MARKETING tren ACC-SW1"},
    )
    assert response.status_code == 200
    assert response.get_json()["change"]["status"] == "pending_approval"


def test_chat_endpoint_blocks_dangerous_requests(
    client, admin_headers, app, access_switch
):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=DANGEROUS_ACTION)
    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "write erase ACC-SW1"}
    )
    assert response.status_code == 403


def test_chat_endpoint_requires_a_message(client, admin_headers, app):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=MONITOR_ACTION)
    assert client.post("/api/ai/chat", headers=admin_headers, json={}).status_code == 422


def test_viewer_can_ask_a_monitor_question(
    client, viewer_headers, app, dist_switch, ssh_factory
):
    ssh_factory.set_client(dist_switch.hostname, default_output="ok")
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=MONITOR_ACTION)
    response = client.post(
        "/api/ai/chat", headers=viewer_headers, json={"message": "check ospf"}
    )
    assert response.status_code == 200


def test_viewer_cannot_create_a_configuration_preview(
    client, viewer_headers, app, access_switch
):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=CONFIGURE_ACTION)
    response = client.post(
        "/api/ai/chat", headers=viewer_headers, json={"message": "tao vlan 25"}
    )
    assert response.status_code == 403
