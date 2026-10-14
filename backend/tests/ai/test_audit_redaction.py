"""Mock-only diagnostics tests; use the existing pre-collection Safety runner.

These tests do not create an app or request database fixtures. Local guards
supplement the runner's disk/database/network protections during collection.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.ai import service as ai_service


@pytest.fixture(autouse=True)
def prohibit_external_operations(monkeypatch):
    from network_copilot.ssh import client as ssh
    from network_copilot.changes import service, batch_service, verification
    from network_copilot.commands import service as commands
    from network_copilot.backups import service as backups

    def forbidden(*args, **kwargs):
        raise AssertionError("Diagnostics mock tests must never access a device, DB or real provider")

    for target, name in ((ssh, "build_client_for_device"), (ssh.SSHClient, "_connect"),
                         (service, "apply"), (batch_service, "apply_batch"),
                         (commands, "execute_readonly"), (backups, "capture_backup"),
                         (verification, "run_verification"),
                         (ai_service.device_service, "get_device_by_hostname"),
                         (ai_service, "build_provider"), (ai_service, "record_event")):
        monkeypatch.setattr(target, name, forbidden)


def collect_provider_context(monkeypatch, results):
    provider = FakeAIProvider()
    service = ai_service.AIService(provider=provider)
    readonly = Mock(return_value=results)
    monkeypatch.setattr(service, "_run_readonly", readonly)
    device = SimpleNamespace(hostname="FAKE-SW", role="access", device_type="cisco_ios", status="online")
    action = SimpleNamespace(explanation="Diagnose the interface and OSPF adjacency")
    operation = SimpleNamespace(commands=[item["command"] for item in results])

    response = service._handle_troubleshoot(action, operation, device, user_id=123)

    readonly.assert_called_once_with(operation, device, 123)
    assert provider.calls == 1
    assert len(provider.prompts) == 1
    prompt = provider.prompts[0]
    assert prompt["mode"] == "explain"
    assert prompt["system_prompt"] == ai_service.EXPLAIN_PROMPT
    assert prompt["user_message"] == action.explanation
    assert prompt["context"]["device"] == vars(device)
    assert response["intent"] == "troubleshoot"
    assert response["requires_approval"] is False
    return provider, prompt["context"]["diagnostics"], response


def test_troubleshoot_preserves_safe_diagnostics_context(monkeypatch):
    results = [
        {"command": "show interfaces Gi0/2", "output": "Interface Gi0/2 is up\nVLAN 25 active"},
        {"command": "show ip ospf neighbor", "output": "OSPF neighbor FULL"},
    ]
    before = deepcopy(results)

    _, diagnostics, response = collect_provider_context(monkeypatch, results)

    assert diagnostics == before
    assert results == before, "Redaction must not mutate collected diagnostics"
    assert response["results"] == before


def test_troubleshoot_redacts_secret_without_losing_safe_lines(monkeypatch):
    fake = "FakeDiagnosticCredential_NotForUse123"
    results = [{
        "command": "show interfaces Gi0/2",
        "output": "\n".join([
            "Interface Gi0/2 is up", "password 7 " + fake,
            "OSPF neighbor FULL", "VLAN 25 active",
        ]),
    }]
    before = deepcopy(results)

    provider, diagnostics, response = collect_provider_context(monkeypatch, results)

    assert fake not in provider.everything_sent()
    assert len(diagnostics) == len(before)
    assert diagnostics[0]["command"] == before[0]["command"]
    output = diagnostics[0]["output"]
    assert "***REDACTED***" in output
    for safe_line in ("Interface Gi0/2 is up", "OSPF neighbor FULL", "VLAN 25 active"):
        assert safe_line in output.splitlines()
    # This test protects the provider boundary, not the authorized raw-results
    # response contract: redaction must leave the collected results unchanged.
    assert results == before
    assert response["results"] == before


@pytest.mark.parametrize("credential", [
    "password\nFakeAlpha",
    'password "FakeAlpha\nFakeBeta"',
], ids=["split-value", "quoted-value"])
def test_troubleshoot_redacts_multiline_secret_and_keeps_diagnostics(monkeypatch, credential):
    results = [{
        "command": "show interfaces Gi0/2",
        "output": "Interface Gi0/2 is up\n" + credential + "\nOSPF neighbor FULL\nVLAN 25 active",
    }]
    before = deepcopy(results)

    provider, diagnostics, response = collect_provider_context(monkeypatch, results)

    assert "FakeAlpha" not in provider.everything_sent()
    assert "FakeBeta" not in provider.everything_sent()
    assert diagnostics[0]["command"] == before[0]["command"]
    output = diagnostics[0]["output"]
    assert "***REDACTED***" in output
    for safe_line in ("Interface Gi0/2 is up", "OSPF neighbor FULL", "VLAN 25 active"):
        assert safe_line in output.splitlines()
    assert results == before
    assert response["results"] == before


@pytest.mark.parametrize("password", ["6543210987654321", "7"], ids=["long-number", "digit-seven"])
def test_troubleshoot_numeric_password_preserves_diagnostics(monkeypatch, password):
    results = [{
        "command": "show interfaces Gi0/2",
        "output": "Interface Gi0/2 is up\npassword " + password + "\nOSPF neighbor FULL\nVLAN 25 active",
    }]
    before = deepcopy(results)

    provider, diagnostics, response = collect_provider_context(monkeypatch, results)

    assert "password " + password not in provider.everything_sent()
    assert diagnostics[0]["command"] == before[0]["command"]
    assert diagnostics[0]["output"].splitlines() == [
        "Interface Gi0/2 is up", "password ***REDACTED***",
        "OSPF neighbor FULL", "VLAN 25 active",
    ]
    assert results == before
    assert response["results"] == before
