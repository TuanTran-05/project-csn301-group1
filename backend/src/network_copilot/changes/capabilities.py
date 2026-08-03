"""Pure capability assessment and recognition contracts."""

import ipaddress
import re
from dataclasses import dataclass
from typing import Literal

CapabilityTier = Literal["level_a_core", "level_a_extended", "best_effort"]
VerificationLevel = Literal["semantic", "best_effort"]

WRAPPERS = {"configure terminal", "end", "exit"}
SAVE_FORMS = {
    "write",
    "write memory",
    "copy running-config startup-config",
}
CORE_FAMILIES = frozenset(
    {
        "vlan",
        "access_port",
        "trunk_port",
        "interface_description",
        "interface_admin_state",
        "interface_ipv4",
        "static_route",
        "save_config",
    }
)
ENABLED_SEMANTIC_FAMILIES = frozenset({"vlan"})


@dataclass(frozen=True)
class OperationExpectation:
    family: str
    data: dict[str, object]

    def to_dict(self) -> dict:
        return {"family": self.family, "data": self.data}


@dataclass(frozen=True)
class CapabilityAssessment:
    capability_tier: CapabilityTier
    verification_level: VerificationLevel
    expectations: tuple[OperationExpectation, ...]

    @property
    def operation_families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.family for item in self.expectations))


def _normalized(command: str) -> str:
    return " ".join(str(command).strip().split())


def _keyword(command: str) -> str:
    return _normalized(command).casefold()


def _fullmatch(pattern: str, command: str) -> re.Match | None:
    return re.fullmatch(pattern, _normalized(command), flags=re.IGNORECASE)


def _expectation(family: str, **data: object) -> OperationExpectation:
    return OperationExpectation(family=family, data=data)


def _parse_interface(command: str) -> str | None:
    match = _fullmatch(r"interface\s+([A-Za-z][A-Za-z-]*\d[\d/.:]*)", command)
    return match.group(1) if match else None


def _parse_vlan_id(value: str) -> int | None:
    vlan_id = int(value)
    return vlan_id if 1 <= vlan_id <= 4094 else None


def _parse_vlan_set(value: str) -> list[int] | None:
    result: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            return None
        if "-" in token:
            parts = token.split("-")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                return None
            start, end = (int(part) for part in parts)
            if start > end or start < 1 or end > 4094:
                return None
            result.update(range(start, end + 1))
        elif token.isdigit():
            vlan_id = _parse_vlan_id(token)
            if vlan_id is None:
                return None
            result.add(vlan_id)
        else:
            return None
    return sorted(result) if result else None


def _prefix_length(mask: str) -> int | None:
    try:
        network = ipaddress.IPv4Network(f"0.0.0.0/{mask}")
    except ValueError:
        return None
    return network.prefixlen


def _parse_vlan(commands: list[str]) -> OperationExpectation | None:
    if len(commands) == 1:
        match = _fullmatch(r"(no\s+)?vlan\s+(\d{1,4})", commands[0])
        if not match:
            return None
        vlan_id = _parse_vlan_id(match.group(2))
        if vlan_id is None:
            return None
        return _expectation(
            "vlan", vlan_id=vlan_id, name=None, present=not bool(match.group(1))
        )
    if len(commands) != 2:
        return None
    vlan_match = _fullmatch(r"vlan\s+(\d{1,4})", commands[0])
    name_match = _fullmatch(r"name\s+(.+)", commands[1])
    if not vlan_match or not name_match:
        return None
    vlan_id = _parse_vlan_id(vlan_match.group(1))
    if vlan_id is None:
        return None
    return _expectation(
        "vlan", vlan_id=vlan_id, name=name_match.group(1), present=True
    )


def _parse_access_port(commands: list[str]) -> OperationExpectation | None:
    if len(commands) != 3:
        return None
    interface = _parse_interface(commands[0])
    mode = _fullmatch(r"switchport\s+mode\s+access", commands[1])
    vlan_match = _fullmatch(r"switchport\s+access\s+vlan\s+(\d{1,4})", commands[2])
    if not interface or not mode or not vlan_match:
        return None
    vlan_id = _parse_vlan_id(vlan_match.group(1))
    return _expectation("access_port", interface=interface, access_vlan=vlan_id) if vlan_id else None


def _parse_trunk_port(commands: list[str]) -> OperationExpectation | None:
    if len(commands) != 3:
        return None
    interface = _parse_interface(commands[0])
    mode = _fullmatch(r"switchport\s+mode\s+trunk", commands[1])
    vlan_match = _fullmatch(r"switchport\s+trunk\s+allowed\s+vlan\s+(.+)", commands[2])
    if not interface or not mode or not vlan_match:
        return None
    allowed_vlans = _parse_vlan_set(vlan_match.group(1))
    return (
        _expectation("trunk_port", interface=interface, allowed_vlans=allowed_vlans)
        if allowed_vlans
        else None
    )


def _parse_description(commands: list[str]) -> OperationExpectation | None:
    if len(commands) != 2:
        return None
    interface = _parse_interface(commands[0])
    description = _fullmatch(r"description\s+(.+)", commands[1])
    remove = _fullmatch(r"no\s+description", commands[1])
    if not interface or (not description and not remove):
        return None
    return _expectation(
        "interface_description",
        interface=interface,
        description=None if remove else description.group(1),
    )


def _parse_admin_state(commands: list[str]) -> OperationExpectation | None:
    if len(commands) != 2:
        return None
    interface = _parse_interface(commands[0])
    state = _fullmatch(r"(no\s+)?shutdown", commands[1])
    if not interface or not state:
        return None
    return _expectation("interface_admin_state", interface=interface, enabled=bool(state.group(1)))


def _parse_interface_ipv4(commands: list[str]) -> OperationExpectation | None:
    if len(commands) != 2:
        return None
    interface = _parse_interface(commands[0])
    address = _fullmatch(
        r"ip\s+address\s+(\S+)\s+(\S+)", commands[1]
    )
    removal = _fullmatch(
        r"no\s+ip\s+address\s+(\S+)\s+(\S+)", commands[1]
    )
    match = address or removal
    if not interface or not match:
        return None
    try:
        ipaddress.IPv4Address(match.group(1))
    except ValueError:
        return None
    prefix_length = _prefix_length(match.group(2))
    if prefix_length is None:
        return None
    return _expectation(
        "interface_ipv4",
        interface=interface,
        address=match.group(1),
        prefix_length=prefix_length,
        present=not bool(removal),
    )


def _parse_static_route(commands: list[str]) -> OperationExpectation | None:
    if len(commands) != 1:
        return None
    match = _fullmatch(
        r"(no\s+)?ip\s+route\s+(\S+)\s+(\S+)\s+(\S+)", commands[0]
    )
    if not match:
        return None
    prefix_length = _prefix_length(match.group(3))
    if prefix_length is None:
        return None
    try:
        network = ipaddress.IPv4Network(
            f"{match.group(2)}/{prefix_length}", strict=False
        )
        next_hop = ipaddress.IPv4Address(match.group(4))
    except ValueError:
        return None
    if str(network.network_address) != match.group(2):
        return None
    return _expectation(
        "static_route",
        network=str(network.network_address),
        next_hop=str(next_hop),
        present=not bool(match.group(1)),
    )


def _parse_save(commands: list[str], execution_mode: str) -> OperationExpectation | None:
    if execution_mode != "exec" or len(commands) != 1:
        return None
    if _keyword(commands[0]) not in SAVE_FORMS:
        return None
    return _expectation(
        "save_config", canonical_command="copy running-config startup-config"
    )


def _strip_wrappers(commands: list[str]) -> list[str] | None:
    normalized = [_normalized(command) for command in commands]
    while normalized and _keyword(normalized[0]) in WRAPPERS:
        normalized.pop(0)
    while normalized and _keyword(normalized[-1]) in WRAPPERS:
        normalized.pop()
    if any(_keyword(command) in WRAPPERS for command in normalized):
        return None
    return normalized


def recognize_change(
    commands: list[str], execution_mode: str
) -> tuple[tuple[OperationExpectation, ...], bool]:
    """Recognize one strict operation family without enabling its verifier."""
    clean = _strip_wrappers(commands)
    if clean is None or not clean:
        return (), True

    parsers = (
        lambda values: _parse_save(values, execution_mode),
        _parse_vlan,
        _parse_access_port,
        _parse_trunk_port,
        _parse_description,
        _parse_admin_state,
        _parse_interface_ipv4,
        _parse_static_route,
    )
    for parser in parsers:
        expectation = parser(clean)
        if expectation is not None:
            return (expectation,), False
    return (), True


def assess_change(
    commands: list[str], execution_mode: str, device_type: str
) -> CapabilityAssessment:
    expectations, unmatched = recognize_change(commands, execution_mode)
    families = {item.family for item in expectations}
    if (
        device_type != "cisco_ios"
        or not expectations
        or unmatched
        or not families.issubset(ENABLED_SEMANTIC_FAMILIES)
    ):
        return CapabilityAssessment("best_effort", "best_effort", ())
    return CapabilityAssessment("level_a_core", "semantic", expectations)
