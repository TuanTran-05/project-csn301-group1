import pytest

from network_copilot.commands.policy import (
    CommandDecision,
    CommandPolicy,
    ai_policy,
    default_policy,
)

policy = CommandPolicy()

ALLOWED = [
    "show ip interface brief",
    "show interfaces status",
    "show vlan brief",
    "show ip route",
    "show ip ospf neighbor",
    "show access-lists",
    "show logging",
    "ping 10.10.10.11",
    "traceroute 10.10.10.11",
]

BLOCKED = [
    "write erase",
    "erase startup-config",
    "reload",
    "delete flash:",
    "debug all",
    "format",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed_commands_are_permitted(command):
    decision = policy.evaluate(command, "core")
    assert decision.allowed is True
    assert isinstance(decision, CommandDecision)


@pytest.mark.parametrize("command", BLOCKED)
def test_dangerous_commands_are_blocked(command):
    decision = policy.evaluate(command, "core")
    assert decision.allowed is False
    assert decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "write erase",
        "WRITE ERASE",
        "  write   erase  ",
        "write memory",
        "copy running-config startup-config",
        "erase flash:",
        "reload in 5",
        "delete flash:config.text",
        "debug ip packet",
        "format flash:",
        "no router ospf 1",
        "shutdown",
        "configure terminal",
        "clear counters",
    ],
)
def test_write_and_destructive_commands_are_blocked(command):
    assert policy.evaluate(command, "core").allowed is False


@pytest.mark.parametrize(
    "command",
    ["sh ip int br", "banana", "show", "", "   ", "exit", "enable"],
)
def test_unknown_commands_default_to_deny(command):
    decision = policy.evaluate(command, "core")
    assert decision.allowed is False
    assert decision.reason


def test_case_and_spacing_are_normalised():
    decision = policy.evaluate("  SHOW   IP   INTERFACE   BRIEF ", "core")
    assert decision.allowed is True
    assert decision.normalized_command == "show ip interface brief"


def test_ping_requires_a_valid_ipv4_address():
    assert policy.evaluate("ping 10.10.10.11", "core").allowed is True
    assert policy.evaluate("ping example.com", "core").allowed is False
    assert policy.evaluate("ping 999.1.1.1", "core").allowed is False
    assert policy.evaluate("ping", "core").allowed is False


def test_traceroute_requires_a_valid_ipv4_address():
    assert policy.evaluate("traceroute 8.8.8.8", "core").allowed is True
    assert policy.evaluate("traceroute google.com", "core").allowed is False


def test_show_running_config_is_allowed_for_backups():
    assert policy.evaluate("show running-config", "core").allowed is True


def test_running_config_is_operator_allowed_but_ai_denied():
    assert default_policy.evaluate("show running-config", "access").allowed is True
    decision = ai_policy.evaluate("show running-config", "access")
    assert decision.allowed is False
    assert "AI-safe" in decision.reason


def test_startup_config_is_never_ai_safe():
    assert ai_policy.evaluate("show startup-config", "access").allowed is False


def test_every_ai_advertised_rule_is_ai_executable():
    for rule in ai_policy.rules:
        assert ai_policy.evaluate(rule.name, "core").allowed or (
            "<ipv4>" in rule.name or "<interface>" in rule.name
        )


def test_chained_commands_are_blocked():
    assert policy.evaluate("show ip route ; write erase", "core").allowed is False
    assert policy.evaluate("show ip route | write erase", "core").allowed is False
    assert policy.evaluate("show ip route && reload", "core").allowed is False


def test_newlines_are_blocked():
    assert policy.evaluate("show ip route\nwrite erase", "core").allowed is False


def test_ospf_command_denied_on_access_role():
    decision = policy.evaluate("show ip ospf neighbor", "access")
    assert decision.allowed is False
    assert "role" in decision.reason.lower()


def test_ospf_command_allowed_on_core_and_distribution():
    assert policy.evaluate("show ip ospf neighbor", "core").allowed is True
    assert policy.evaluate("show ip ospf neighbor", "distribution").allowed is True


def test_vlan_command_denied_on_isp_router():
    assert policy.evaluate("show vlan brief", "isp").allowed is False


def test_decision_reports_the_matched_rule():
    decision = policy.evaluate("show ip route", "core")
    assert decision.matched_rule == "show ip route"


def test_evaluate_rejects_non_string_input():
    assert policy.evaluate(None, "core").allowed is False
