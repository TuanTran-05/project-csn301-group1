from copy import deepcopy

import pytest
from pydantic import ValidationError as PydanticValidationError
from fakes.fake_ai_provider import FakeAIProvider

from network_copilot.ai.schemas import AIAction
from network_copilot.ai.service import AIService
from network_copilot.changes.model import ChangeRequest
from network_copilot.commands.model import CommandExecution
from network_copilot.credentials.service import store_device_credential
from network_copilot.errors import PolicyViolationError, ValidationError
from network_copilot.extensions import db

MONITOR_ACTION = {
    "intent": "monitor",
    "operations": [{
        "device_hostnames": ["DIST-SW1"],
        "execution_mode": "exec",
        "commands": ["show ip ospf neighbor"],
        "verification_commands": [],
    }],
    "explanation": "Checking OSPF adjacencies on DIST-SW1.",
}

CONFIGURE_ACTION = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["ACC-SW1"],
        "execution_mode": "config",
        "commands": ["vlan 25", "name MARKETING"],
        "verification_commands": ["show vlan brief"],
    }],
    "explanation": "Creating VLAN 25 named MARKETING on ACC-SW1.",
}

DANGEROUS_ACTION = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["ACC-SW1"],
        "execution_mode": "exec",
        "commands": ["write erase"],
        "verification_commands": [],
    }],
    "explanation": "Wiping the configuration.",
}

WRITE_ALL_ACTION = {
    "intent": "configure",
    "operations": [{
        "device_hostnames": ["*"],
        "execution_mode": "exec",
        "commands": ["write memory"],
        "verification_commands": [],
    }],
    "explanation": "Luu cau hinh tren tat ca thiet bi.",
}

HETEROGENEOUS_CONFIGURE_ACTION = {
    "intent": "configure",
    "operations": [
        {
            "device_hostnames": ["ACC-SW1", "CORE-SW1"],
            "execution_mode": "exec",
            "commands": ["write memory"],
            "verification_commands": ["show startup-config"],
        },
        {
            "device_hostnames": ["DIST-SW1"],
            "execution_mode": "config",
            "commands": ["vlan 220", "name VOICE"],
            "verification_commands": ["show vlan brief"],
        },
    ],
    "explanation": "Save access and core configs, then create the voice VLAN on distribution.",
}

OSPF_OUTPUT = """Neighbor ID     Pri   State           Dead Time   Address         Interface
2.2.2.2           1   FULL/DR         00:00:33    10.255.0.6      GigabitEthernet0/2
"""


def service_with(app, payload) -> tuple[AIService, FakeAIProvider]:
    provider = FakeAIProvider(responses=payload)
    return AIService(provider=provider), provider


# -- schema ---------------------------------------------------------------


def test_action_schema_accepts_multi_target_operations():
    action = AIAction(**WRITE_ALL_ACTION)
    assert action.operations[0].device_hostnames == ["*"]
    assert action.operations[0].execution_mode == "exec"


def test_wildcard_cannot_be_mixed_with_explicit_hostname():
    payload = deepcopy(WRITE_ALL_ACTION)
    payload["operations"][0]["device_hostnames"] = ["*", "ACC-SW1"]
    with pytest.raises(PydanticValidationError):
        AIAction(**payload)


def test_ai_action_schema_accepts_a_valid_payload():
    action = AIAction(**MONITOR_ACTION)
    assert action.intent == "monitor"
    assert action.operations[0].device_hostnames == ["DIST-SW1"]
    assert action.operations[0].commands == ["show ip ospf neighbor"]


def test_ai_action_defaults_verification_commands_to_empty():
    action = AIAction(
        intent="monitor",
        operations=[{
            "device_hostnames": ["CORE-SW1"],
            "execution_mode": "exec",
            "commands": ["show ip route"],
        }],
        explanation="Reading the routing table.",
    )
    assert action.operations[0].verification_commands == []


@pytest.mark.parametrize("intent", ["reboot", "delete", "", "MONITOR"])
def test_ai_action_rejects_unknown_intents(intent):
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        AIAction(
            intent=intent,
            operations=[{
                "device_hostnames": ["CORE-SW1"],
                "execution_mode": "exec",
                "commands": ["show ip route"],
            }],
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


def test_prompt_tells_the_model_stale_status_is_not_a_reason_to_refuse(
    app, dist_switch, admin_user
):
    service, provider = service_with(app, MONITOR_ACTION)
    service.interpret("Kiem tra OSPF cua DIST-SW1", admin_user.id)
    prompt = provider.prompts[0]["system_prompt"]
    assert "stale" in prompt
    assert "never a reason to skip commands" in prompt


def test_prompt_allows_arbitrary_configuration_and_never_delegates_risk(
    app, admin_user
):
    service, provider = service_with(app, WRITE_ALL_ACTION)
    service.interpret("thuc hien lenh write tren toan bo thiet bi", admin_user.id)
    prompt = provider.prompts[0]["system_prompt"]
    assert "any Cisco CLI" in prompt
    assert "risk" in prompt and "backend" in prompt
    assert "only for the changes listed in supported_actions" not in prompt


# -- tolerating real model output ----------------------------------------
# Even with response_mime_type=application/json, models sometimes emit more
# than one JSON value or wrap the object in prose. Always take the first
# complete object: merging several proposed actions would be unsafe.


def test_interpret_takes_the_first_of_two_concatenated_objects(app, admin_user):
    import json as _json

    payload = _json.dumps(MONITOR_ACTION) + "\n" + _json.dumps(CONFIGURE_ACTION)
    service, _ = service_with(app, payload)
    action = service.interpret("hello", admin_user.id)
    assert action.intent == "monitor"
    assert action.operations[0].device_hostnames == ["DIST-SW1"]


def test_interpret_ignores_trailing_prose(app, admin_user):
    import json as _json

    service, _ = service_with(
        app, _json.dumps(MONITOR_ACTION) + "\n\nHy vong dieu nay huu ich!"
    )
    assert service.interpret("hello", admin_user.id).intent == "monitor"


def test_interpret_ignores_leading_prose(app, admin_user):
    import json as _json

    service, _ = service_with(
        app, "Day la ke hoach cua toi:\n" + _json.dumps(MONITOR_ACTION)
    )
    assert service.interpret("hello", admin_user.id).intent == "monitor"


def test_interpret_handles_a_fenced_object_with_trailing_text(app, admin_user):
    import json as _json

    service, _ = service_with(
        app, "```json\n" + _json.dumps(MONITOR_ACTION) + "\n```\nDone."
    )
    assert service.interpret("hello", admin_user.id).intent == "monitor"


def test_interpret_preserves_nested_objects(app, admin_user):
    import json as _json

    nested = dict(MONITOR_ACTION, explanation="see {details} here")
    service, _ = service_with(app, _json.dumps(nested) + _json.dumps(CONFIGURE_ACTION))
    action = service.interpret("hello", admin_user.id)
    assert action.explanation == "see {details} here"


def test_interpret_rejects_a_json_array(app, admin_user):
    service, _ = service_with(app, '[{"intent": "monitor"}]')
    with pytest.raises(ValidationError):
        service.interpret("hello", admin_user.id)


def test_interpret_rejects_an_empty_response(app, admin_user):
    service, _ = service_with(app, "")
    with pytest.raises(ValidationError):
        service.interpret("hello", admin_user.id)


# -- the model declining the request --------------------------------------
# A good model refuses a dangerous request itself and answers with no
# commands. That is not a schema fault, and saying so confuses the operator.

DECLINED_ACTION = {
    "intent": "monitor",
    "operations": [],
    "explanation": "Lenh 'write erase' khong duoc ho tro vi no xoa cau hinh.",
}


def test_declined_operation_list_surfaces_model_explanation(app, admin_user):
    service, _ = service_with(
        app,
        {
            "intent": "configure",
            "operations": [],
            "explanation": "Khong the tao de xuat.",
        },
    )
    with pytest.raises(ValidationError, match="Khong the tao de xuat"):
        service.interpret("configure", admin_user.id)


def test_interpret_retries_once_when_the_model_returns_broken_json(
    app, dist_switch, admin_user
):
    """Observed with gemini-3.5-flash: a response occasionally repeats a
    fragment and stops being valid JSON. One retry clears it."""
    import json as _json

    provider = FakeAIProvider(responses=['{"broken', _json.dumps(MONITOR_ACTION)])
    action = AIService(provider=provider).interpret("hello", admin_user.id)
    assert action.intent == "monitor"
    assert provider.calls == 2


def test_interpret_gives_up_after_one_retry(app, admin_user):
    provider = FakeAIProvider(responses=['{"broken', '{"still broken'])
    with pytest.raises(ValidationError):
        AIService(provider=provider).interpret("hello", admin_user.id)
    assert provider.calls == 2


def test_a_valid_response_is_never_retried(app, dist_switch, admin_user):
    provider = FakeAIProvider(responses=MONITOR_ACTION)
    AIService(provider=provider).interpret("hello", admin_user.id)
    assert provider.calls == 1


def test_a_declined_request_is_not_retried(app, access_switch, admin_user):
    """A refusal is a real answer. Asking again just costs money."""
    provider = FakeAIProvider(
        responses={
            "intent": "monitor",
            "operations": [],
            "explanation": "Khong ho tro lenh nay.",
        }
    )
    with pytest.raises(ValidationError):
        AIService(provider=provider).interpret("write erase", admin_user.id)
    assert provider.calls == 1


def test_declined_request_reports_the_models_reason(app, access_switch, admin_user):
    service, _ = service_with(app, DECLINED_ACTION)
    with pytest.raises(ValidationError) as exc:
        service.interpret("write erase tren ACC-SW1", admin_user.id)
    assert "khong duoc ho tro" in exc.value.message


def test_declined_request_is_not_reported_as_a_schema_fault(
    app, access_switch, admin_user
):
    service, _ = service_with(app, DECLINED_ACTION)
    with pytest.raises(ValidationError) as exc:
        service.interpret("write erase tren ACC-SW1", admin_user.id)
    assert "schema" not in exc.value.message.lower()


def test_declined_request_never_touches_a_device(
    app, access_switch, admin_user, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    service, _ = service_with(app, DECLINED_ACTION)
    with pytest.raises(ValidationError):
        service.handle("write erase tren ACC-SW1", admin_user.id)
    assert fake.calls == []


def test_chat_endpoint_reports_a_declined_request_as_422(
    client, admin_headers, app, access_switch
):
    from fakes.fake_ai_provider import FakeAIProvider as _Fake

    app.config["AI_PROVIDER_INSTANCE"] = _Fake(responses=DECLINED_ACTION)
    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "write erase ACC-SW1"}
    )
    assert response.status_code == 422
    assert "khong duoc ho tro" in response.get_json()["message"]


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


@pytest.mark.parametrize(
    ("operations", "error"),
    [
        (
            [
                {
                    "device_hostnames": ["DIST-SW1"],
                    "execution_mode": "exec",
                    "commands": ["show ip route"],
                    "verification_commands": [],
                },
                {
                    "device_hostnames": ["ACC-SW1"],
                    "execution_mode": "exec",
                    "commands": ["show ip route"],
                    "verification_commands": [],
                },
            ],
            "exactly one operation",
        ),
        (
            [{
                "device_hostnames": ["*"],
                "execution_mode": "exec",
                "commands": ["show ip route"],
                "verification_commands": [],
            }],
            "one explicit hostname",
        ),
        (
            [{
                "device_hostnames": ["DIST-SW1"],
                "execution_mode": "config",
                "commands": ["show ip route"],
                "verification_commands": [],
            }],
            "EXEC mode",
        ),
    ],
    ids=["multiple-operations", "wildcard-target", "config-mode"],
)
def test_readonly_intent_rejects_an_unsupported_operation_shape(
    app, admin_user, dist_switch, access_switch, ssh_factory, operations, error
):
    service, _ = service_with(
        app,
        {
            "intent": "monitor",
            "operations": operations,
            "explanation": "Checking routes.",
        },
    )
    with pytest.raises(ValidationError, match=error):
        service.handle("check routes", admin_user.id)
    assert ssh_factory.clients == {}


# -- configure intent -----------------------------------------------------


def test_vietnamese_write_all_request_creates_batch_without_ssh(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    service, _ = service_with(app, WRITE_ALL_ACTION)
    result = service.handle(
        "thuc hien lenh write tren toan bo thiet bi", admin_user.id
    )
    assert result["intent"] == "configure"
    assert result["batch"]["confirmation_text"] == "CONFIRM ALL"
    assert len(result["batch"]["changes"]) == 2
    assert ssh_factory.clients == {}


def test_configure_intent_only_creates_a_preview(
    app, access_switch, admin_user, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    service, _ = service_with(app, CONFIGURE_ACTION)
    result = service.handle("Tao VLAN 25 MARKETING tren ACC-SW1", admin_user.id)

    assert result["intent"] == "configure"
    assert result["batch"]["status"] == "pending_approval"
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
    assert change.commands == [
        "configure terminal",
        "vlan 25",
        "name MARKETING",
        "end",
    ]


def test_configure_ai_freezes_disjoint_heterogeneous_operations_into_children(
    app, admin_user, access_switch, core_switch, dist_switch, ssh_factory
):
    service, _ = service_with(app, HETEROGENEOUS_CONFIGURE_ACTION)

    result = service.handle("save configs and create the voice VLAN", admin_user.id)

    batch = result["batch"]
    persisted = {
        change.device.hostname: change
        for change in db.session.query(ChangeRequest).all()
    }
    assert batch["status"] == "pending_approval"
    assert batch["source"] == "ai"
    assert set(persisted) == {"ACC-SW1", "CORE-SW1", "DIST-SW1"}
    for hostname in ("ACC-SW1", "CORE-SW1"):
        child = persisted[hostname]
        assert child.execution_mode == "exec"
        assert child.commands == ["write memory"]
        assert child.verification_commands == ["show startup-config"]
    dist_child = persisted["DIST-SW1"]
    assert dist_child.execution_mode == "config"
    assert dist_child.commands == [
        "configure terminal",
        "vlan 220",
        "name VOICE",
        "end",
    ]
    assert dist_child.verification_commands == ["show vlan brief"]
    assert ssh_factory.clients == {}


def test_configure_intent_requires_approval_before_anything_runs(
    app, access_switch, admin_user
):
    service, _ = service_with(app, CONFIGURE_ACTION)
    result = service.handle("Tao VLAN 25", admin_user.id)
    assert result["requires_approval"] is True


# -- dangerous commands ---------------------------------------------------


def test_write_erase_from_ai_requires_confirmation_not_a_block(
    app, access_switch, admin_user, ssh_factory
):
    """The AI has full authority to propose a dangerous command: it still
    only creates a Preview (never touches SSH), but that Preview is flagged
    so Apply will demand the device hostname be typed back."""
    fake = ssh_factory.set_client(access_switch.hostname)
    service, _ = service_with(app, DANGEROUS_ACTION)

    result = service.handle("Xoa cau hinh ACC-SW1", admin_user.id)

    assert result["intent"] == "configure"
    assert result["batch"]["status"] == "pending_approval"
    assert result["batch"]["requires_confirmation"] is True
    assert fake.calls == []
    assert db.session.query(ChangeRequest).count() == 1


def test_monitor_intent_with_a_dangerous_command_is_blocked(
    app, access_switch, admin_user, ssh_factory
):
    fake = ssh_factory.set_client(access_switch.hostname)
    service, _ = service_with(
        app,
        {
            "intent": "monitor",
            "operations": [{
                "device_hostnames": ["ACC-SW1"],
                "execution_mode": "exec",
                "commands": ["reload"],
                "verification_commands": [],
            }],
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
            "operations": [{
                "device_hostnames": ["GHOST-SW"],
                "execution_mode": "exec",
                "commands": ["show ip route"],
                "verification_commands": [],
            }],
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
    assert "supported_actions" not in context


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
            "operations": [{
                "device_hostnames": ["DIST-SW1"],
                "execution_mode": "exec",
                "commands": ["show ip ospf neighbor"],
                "verification_commands": [],
            }],
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
            "operations": [{
                "device_hostnames": ["DIST-SW1"],
                "execution_mode": "exec",
                "commands": ["show ip ospf neighbor"],
                "verification_commands": [],
            }],
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
    assert response.get_json()["batch"]["status"] == "pending_approval"


def test_chat_endpoint_flags_dangerous_requests_for_confirmation(
    client, admin_headers, app, access_switch
):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=DANGEROUS_ACTION)
    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "write erase ACC-SW1"}
    )
    assert response.status_code == 200
    assert response.get_json()["batch"]["requires_confirmation"] is True


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


CHAT_ACTION = {
    "intent": "chat",
    "operations": [],
    "explanation": "Chao ban! Toi co the giup kiem tra va cau hinh thiet bi mang.",
}


def test_ai_action_accepts_a_chat_intent_with_no_operations():
    action = AIAction(**CHAT_ACTION)
    assert action.intent == "chat"
    assert action.operations == []


def test_ai_action_rejects_a_chat_intent_carrying_operations():
    payload = deepcopy(CHAT_ACTION)
    payload["operations"] = [
        {
            "device_hostnames": ["DIST-SW1"],
            "execution_mode": "exec",
            "commands": ["show ip ospf neighbor"],
            "verification_commands": [],
        }
    ]
    with pytest.raises(PydanticValidationError):
        AIAction(**payload)


def test_ai_action_rejects_chat_without_operations():
    """operations stays a required key: the provider schema declares it
    required, so a chat action sends an explicit empty list rather than
    omitting the field."""
    with pytest.raises(PydanticValidationError):
        AIAction(intent="chat", explanation="hello")


@pytest.mark.parametrize("intent", ["monitor", "configure", "troubleshoot"])
def test_ai_action_still_rejects_an_action_intent_with_no_operations(intent):
    """Relaxing operations for chat must not relax it for the intents that
    actually run commands."""
    with pytest.raises(PydanticValidationError):
        AIAction(intent=intent, operations=[], explanation="x")


def test_provider_schema_offers_the_chat_intent():
    from network_copilot.ai.schemas import build_ai_action_schema

    schema = build_ai_action_schema(["DIST-SW1"])
    assert "chat" in schema["properties"]["intent"]["enum"]


def test_handle_returns_a_chat_reply_without_touching_devices(
    app, admin_user, ssh_factory
):
    service, _ = service_with(app, CHAT_ACTION)

    result = service.handle("alo", admin_user.id)

    assert result["intent"] == "chat"
    assert result["explanation"] == CHAT_ACTION["explanation"]
    assert result["requires_approval"] is False
    assert "results" not in result
    assert "change" not in result
    assert "batch" not in result


def test_handle_chat_never_opens_an_ssh_session(app, admin_user, ssh_factory):
    service, _ = service_with(app, CHAT_ACTION)
    service.handle("alo", admin_user.id)
    assert ssh_factory.clients == {}


def test_handle_chat_creates_no_change_or_batch(app, admin_user, ssh_factory):
    from network_copilot.changes.model import ChangeBatch

    service, _ = service_with(app, CHAT_ACTION)
    service.handle("alo", admin_user.id)

    assert db.session.query(ChangeRequest).count() == 0
    assert db.session.query(ChangeBatch).count() == 0


def test_handle_chat_writes_no_audit_row(app, admin_user, ssh_factory):
    """Deliberate: audit_logs traces operations against devices, and a
    greeting performs none. The turn is still fully recorded in
    chat_messages."""
    from network_copilot.audit.model import AuditLog

    service, _ = service_with(app, CHAT_ACTION)
    service.handle("alo", admin_user.id)

    assert db.session.query(AuditLog).count() == 0


def test_interpret_still_refuses_an_empty_action_intent(app, admin_user):
    refusal = {
        "intent": "monitor",
        "operations": [],
        "explanation": "Khong tim thay thiet bi phu hop.",
    }
    service, _ = service_with(app, refusal)
    with pytest.raises(ValidationError):
        service.interpret("kiem tra thiet bi khong ton tai", admin_user.id)


def test_prompt_forbids_inventing_live_device_state_in_chat(app, admin_user):
    service, provider = service_with(app, CHAT_ACTION)
    service.interpret("alo", admin_user.id)
    prompt = provider.prompts[0]["system_prompt"]
    assert "invent the live state" in prompt
