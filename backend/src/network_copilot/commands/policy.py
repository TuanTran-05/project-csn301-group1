"""Command Policy Engine.

Every command that reaches a device is evaluated here first. The engine is an
allowlist: anything it does not explicitly recognise is denied. It runs before
any SSH session is opened, so a blocked command never touches the network.
"""

import ipaddress
import re
from dataclasses import dataclass
from typing import Callable

ALL_ROLES = frozenset(
    {"isp", "firewall", "core", "distribution", "access", "dmz", "management"}
)
ROUTING_ROLES = frozenset({"core", "distribution"})
SWITCHING_ROLES = frozenset({"core", "distribution", "access", "dmz", "management"})

# Characters that would let a caller staple a second command onto an allowed one.
SHELL_METACHARACTERS = (";", "|", "&", "\n", "\r", "$(", "`", ">", "<")

# Commands that must never run, regardless of role. Matched on the normalised
# command with a word boundary so "show logging" is not caught by "log".
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^write\b", "write commands modify or erase device configuration"),
    (r"^erase\b", "erase destroys device configuration"),
    (r"^reload\b", "reload restarts the device"),
    (r"^delete\b", "delete removes files from device storage"),
    (r"^format\b", "format wipes device storage"),
    (r"^debug\b", "debug can overwhelm the device CPU"),
    (r"^undebug\b", "debug control is not permitted"),
    (r"^copy\b", "copy can overwrite the startup configuration"),
    (r"^configure\b", "configuration must go through the change workflow"),
    (r"^conf\s+t\b", "configuration must go through the change workflow"),
    (r"^no\s+", "negation commands remove configuration"),
    (r"^shutdown\b", "shutdown disables an interface"),
    (r"^clear\b", "clear discards operational state"),
    (r"^reset\b", "reset disrupts device operation"),
    (r"^boot\b", "boot changes the device boot sequence"),
    (r"^archive\b", "archive can overwrite configuration"),
    (r"^setup\b", "setup restarts initial configuration"),
    (r"^tclsh\b", "scripting shells are not permitted"),
    (r"^squeeze\b", "flash maintenance is not permitted"),
)


def _is_ipv4(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def _exact(expected: str) -> Callable[[str], bool]:
    return lambda command: command == expected


def _ipv4_command(verb: str) -> Callable[[str], bool]:
    pattern = re.compile(rf"^{verb} (\S+)$")

    def matcher(command: str) -> bool:
        match = pattern.match(command)
        return bool(match) and _is_ipv4(match.group(1))

    return matcher

def _switchport_detail(command: str) -> bool:
    from ..parsers.switchports import normalize_interface_name
    match = re.fullmatch(r"show interfaces (\S+) switchport", command, re.I)
    if not match: return False
    try: normalize_interface_name(match.group(1)); return True
    except ValueError: return False


@dataclass(frozen=True)
class CommandRule:
    name: str
    matcher: Callable[[str], bool]
    allowed_roles: frozenset[str] = ALL_ROLES
    description: str = ""


@dataclass(frozen=True)
class CommandDecision:
    allowed: bool
    normalized_command: str
    reason: str = ""
    matched_rule: str | None = None


READ_ONLY_RULES: tuple[CommandRule, ...] = (
    CommandRule(
        "show ip interface brief",
        _exact("show ip interface brief"),
        description="Interface addressing and line state",
    ),
    CommandRule(
        "show interfaces status",
        _exact("show interfaces status"),
        description="Switchport status",
    ),
    CommandRule("show interfaces <interface> switchport", _switchport_detail, SWITCHING_ROLES, "Switchport detail"),
    CommandRule("show interfaces trunk", _exact("show interfaces trunk"), SWITCHING_ROLES, "Trunk state"),
    CommandRule("show ip dhcp pool", _exact("show ip dhcp pool"), ROUTING_ROLES, "DHCP pools"),
    CommandRule(
        "show vlan brief",
        _exact("show vlan brief"),
        SWITCHING_ROLES,
        "VLAN database",
    ),
    CommandRule(
        "show ip route", _exact("show ip route"), description="IPv4 routing table"
    ),
    # Cisco ASA spellings. Deliberately ungated by role or device type: the
    # policy engine's question is "is this read-only and safe", which is the
    # same answer everywhere. An IOS device asked for "show route" simply
    # returns its own syntax error, which is the symmetric case of what an
    # ASA does with IOS syntax today and is no worse.
    CommandRule(
        "show interface ip brief",
        _exact("show interface ip brief"),
        description="ASA interface addressing and line state",
    ),
    CommandRule(
        "show route", _exact("show route"), description="ASA IPv4 routing table"
    ),
    CommandRule(
        "show access-list",
        _exact("show access-list"),
        description="ASA ACL definitions",
    ),
    CommandRule(
        "show ip ospf neighbor",
        _exact("show ip ospf neighbor"),
        ROUTING_ROLES,
        "OSPF adjacencies",
    ),
    CommandRule(
        "show access-lists", _exact("show access-lists"), description="ACL definitions"
    ),
    CommandRule("show logging", _exact("show logging"), description="Device log buffer"),
    CommandRule(
        "show running-config",
        _exact("show running-config"),
        description="Running configuration, used for pre-change backups",
    ),
    CommandRule(
        "show version", _exact("show version"), description="Platform and image details"
    ),
    CommandRule("show clock", _exact("show clock"), description="Device clock"),
    CommandRule(
        "ping <ipv4>", _ipv4_command("ping"), description="ICMP reachability test"
    ),
    CommandRule(
        "traceroute <ipv4>",
        _ipv4_command("traceroute"),
        description="Hop-by-hop path trace",
    ),
)


class CommandPolicy:
    """Allowlist-based evaluator. Unknown commands are always denied."""

    def __init__(self, rules: tuple[CommandRule, ...] = READ_ONLY_RULES):
        self.rules = rules

    @staticmethod
    def normalize(command: str) -> str:
        return re.sub(r"\s+", " ", command.strip()).lower()

    def evaluate(self, command, device_role: str) -> CommandDecision:
        if not isinstance(command, str) or not command.strip():
            return CommandDecision(
                allowed=False,
                normalized_command="",
                reason="A non-empty command string is required.",
            )

        # Check metacharacters on the raw string: normalising collapses newlines.
        for token in SHELL_METACHARACTERS:
            if token in command:
                return CommandDecision(
                    allowed=False,
                    normalized_command=self.normalize(command),
                    reason=(
                        f"Command contains the forbidden character sequence {token!r}; "
                        "chained commands are not permitted."
                    ),
                )

        normalized = self.normalize(command)

        for pattern, explanation in DANGEROUS_PATTERNS:
            if re.search(pattern, normalized):
                return CommandDecision(
                    allowed=False,
                    normalized_command=normalized,
                    reason=f"Command is blocked: {explanation}.",
                )

        for rule in self.rules:
            if not rule.matcher(normalized):
                continue
            if device_role not in rule.allowed_roles:
                return CommandDecision(
                    allowed=False,
                    normalized_command=normalized,
                    reason=(
                        f"Command '{rule.name}' is not permitted on a device with "
                        f"role '{device_role}'."
                    ),
                    matched_rule=rule.name,
                )
            return CommandDecision(
                allowed=True, normalized_command=normalized, matched_rule=rule.name
            )

        return CommandDecision(
            allowed=False,
            normalized_command=normalized,
            reason=(
                "Command is not on the read-only allowlist. "
                "Unknown commands are denied by default."
            ),
        )

    def allowed_commands(self, device_role: str) -> list[str]:
        """Rule names available for a role, used to build AI context."""
        return [rule.name for rule in self.rules if device_role in rule.allowed_roles]


default_policy = CommandPolicy()

AI_EXCLUDED_RULE_NAMES = frozenset({"show running-config", "show startup-config"})
AI_SAFE_READ_ONLY_RULES = tuple(
    rule for rule in READ_ONLY_RULES if rule.name not in AI_EXCLUDED_RULE_NAMES
)


class AIAwareCommandPolicy(CommandPolicy):
    """Read-only policy with explicit protection for configuration dumps.

    The operator/API policy intentionally keeps ``show running-config`` for
    trusted backup and verification paths. AI-originated reads use this
    separate policy so hiding a command from prompt context is not mistaken
    for enforcement.
    """

    def evaluate(self, command, device_role: str) -> CommandDecision:
        if isinstance(command, str):
            normalized = self.normalize(command)
            if normalized in AI_EXCLUDED_RULE_NAMES:
                return CommandDecision(
                    allowed=False,
                    normalized_command=normalized,
                    reason=(
                        "Command is blocked by the AI-safe read-only policy: "
                        "full configuration output cannot be sent to the model."
                    ),
                )
        return super().evaluate(command, device_role)


ai_policy = AIAwareCommandPolicy(AI_SAFE_READ_ONLY_RULES)


def policy_for_source(source: str) -> CommandPolicy:
    return ai_policy if source == "ai" else default_policy
