"""Configuration change workflow: Preview -> Approve -> Apply -> Verify.

The MVP deliberately supports a narrow set of configuration templates. Anything
the templates do not recognise is refused, so an AI-generated or hand-typed
command can never reach a device unless it is one of the shapes below:

  * create or rename a VLAN
  * assign an access port to a VLAN
  * set an interface description
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..devices import service as device_service
from ..devices.model import Device
from ..errors import NotFoundError, PolicyViolationError, ValidationError
from ..extensions import db
from .model import ChangeRequest

# VLANs that must never be created, renamed or deleted.
SYSTEM_VLANS = {1, 1002, 1003, 1004, 1005}
USER_VLAN_RANGE = range(2, 1002)

CONFIG_ENTER = "configure terminal"
CONFIG_EXIT = "end"
WRAPPERS = {CONFIG_ENTER, CONFIG_EXIT, "exit"}

CRITICAL_ROLES = {"core", "distribution"}


# -- supported templates --------------------------------------------------


@dataclass(frozen=True)
class ConfigTemplate:
    name: str
    pattern: re.Pattern
    canonical: Callable[[re.Match], str]


TEMPLATES: tuple[ConfigTemplate, ...] = (
    ConfigTemplate(
        "config_enter", re.compile(r"^configure terminal$", re.I), lambda m: CONFIG_ENTER
    ),
    ConfigTemplate("config_exit", re.compile(r"^end$", re.I), lambda m: CONFIG_EXIT),
    ConfigTemplate("config_up", re.compile(r"^exit$", re.I), lambda m: "exit"),
    ConfigTemplate(
        "vlan_select",
        re.compile(r"^vlan (?P<vlan_id>\d{1,4})$", re.I),
        lambda m: f"vlan {int(m.group('vlan_id'))}",
    ),
    ConfigTemplate(
        "vlan_name",
        re.compile(r"^name (?P<name>[\w\-]{1,32})$", re.I),
        lambda m: f"name {m.group('name')}",
    ),
    ConfigTemplate(
        "interface_select",
        re.compile(r"^interface (?P<interface>[A-Za-z][\w\-]*[\d/.:]+)$", re.I),
        lambda m: f"interface {m.group('interface')}",
    ),
    ConfigTemplate(
        "switchport_mode_access",
        re.compile(r"^switchport mode access$", re.I),
        lambda m: "switchport mode access",
    ),
    ConfigTemplate(
        "switchport_access_vlan",
        re.compile(r"^switchport access vlan (?P<vlan_id>\d{1,4})$", re.I),
        lambda m: f"switchport access vlan {int(m.group('vlan_id'))}",
    ),
    ConfigTemplate(
        "interface_description",
        re.compile(r"^description (?P<text>[\w\-\. ]{1,200})$", re.I),
        lambda m: f"description {m.group('text').strip()}",
    ),
)


@dataclass(frozen=True)
class BlockRule:
    pattern: re.Pattern
    reason: str
    roles: frozenset[str] | None = None  # None means every role

    def applies_to(self, role: str) -> bool:
        return self.roles is None or role in self.roles


BLOCK_RULES: tuple[BlockRule, ...] = (
    BlockRule(
        re.compile(r"^no\s+router\s+ospf\b", re.I),
        "Removing an OSPF process would tear down routing across the lab.",
    ),
    BlockRule(
        re.compile(r"^(no\s+)?shutdown$", re.I),
        "Shutting an interface on a core or distribution switch would take down an "
        "uplink.",
        frozenset(CRITICAL_ROLES),
    ),
    BlockRule(
        re.compile(r"^no\s+vlan\s+(?P<vlan_id>\d{1,4})$", re.I),
        "System VLANs cannot be deleted.",
    ),
    BlockRule(
        re.compile(r"^no\s+switchport\b", re.I),
        "Removing switchport configuration is outside the supported templates.",
    ),
)


def _normalise(command: str) -> str:
    return re.sub(r"\s+", " ", str(command).strip())


def _check_block_rules(command: str, role: str) -> None:
    for rule in BLOCK_RULES:
        match = rule.pattern.match(command)
        if match is None or not rule.applies_to(role):
            continue
        # "no vlan <id>" is only blocked outright for system VLANs; user VLANs
        # fall through and are refused by the default-deny below.
        if "vlan_id" in (match.groupdict() or {}):
            if int(match.group("vlan_id")) not in SYSTEM_VLANS:
                continue
        raise PolicyViolationError(rule.reason, {"command": command})


def _match_template(command: str) -> tuple[ConfigTemplate, re.Match] | None:
    for template in TEMPLATES:
        match = template.pattern.match(command)
        if match is not None:
            return template, match
    return None


def _validate_vlan_id(template_name: str, match: re.Match, command: str) -> None:
    if "vlan_id" not in (match.groupdict() or {}):
        return
    vlan_id = int(match.group("vlan_id"))
    if vlan_id in SYSTEM_VLANS or vlan_id not in USER_VLAN_RANGE:
        raise PolicyViolationError(
            f"VLAN {vlan_id} is reserved. Only VLANs 2-1001 may be changed.",
            {"command": command},
        )


def validate_commands(commands: list[str], device: Device) -> list[str]:
    """Return canonical commands, or raise when anything is unsupported."""
    if not commands or not isinstance(commands, (list, tuple)):
        raise ValidationError("At least one configuration command is required.")

    canonical: list[str] = []
    for raw in commands:
        command = _normalise(raw)
        if not command:
            continue

        _check_block_rules(command, device.role)

        matched = _match_template(command)
        if matched is None:
            raise PolicyViolationError(
                f"Command '{command}' is not one of the supported configuration "
                "templates (VLAN create/rename, access port assignment, interface "
                "description).",
                {"command": command},
            )

        template, match = matched
        _validate_vlan_id(template.name, match, command)
        canonical.append(template.canonical(match))

    if not canonical:
        raise ValidationError("At least one configuration command is required.")
    return canonical


def _wrap(commands: list[str]) -> list[str]:
    """Strip any caller-supplied wrappers and apply exactly one config block."""
    body = [command for command in commands if command not in WRAPPERS]
    if not body:
        raise ValidationError(
            "The change contains no configuration commands, only mode wrappers."
        )
    return [CONFIG_ENTER, *body, CONFIG_EXIT]


# -- derived preview content ---------------------------------------------


def derive_verification_commands(commands: list[str], device: Device) -> list[str]:
    """Read-only commands that prove the change landed."""
    from ..commands.policy import default_policy

    derived: list[str] = []

    def add(command: str) -> None:
        if command in derived:
            return
        if default_policy.evaluate(command, device.role).allowed:
            derived.append(command)

    for command in commands:
        if command.startswith("vlan ") or command.startswith("switchport access vlan"):
            add("show vlan brief")
        if command.startswith("name "):
            add("show vlan brief")
        if command.startswith("interface ") or command.startswith("description "):
            add("show interfaces status")

    if not derived:
        add("show running-config")
    return derived


def derive_rollback_commands(commands: list[str]) -> list[str]:
    """Best-effort inverse of the change. The MVP never runs these itself."""
    rollback: list[str] = [CONFIG_ENTER]
    current_interface: str | None = None

    for command in commands:
        if command.startswith("interface "):
            current_interface = command
            rollback.append(command)
        elif command.startswith("vlan "):
            rollback.append(f"no {command}")
        elif command.startswith("name "):
            rollback.append(
                "! previous VLAN name must be restored manually if the VLAN existed"
            )
        elif command.startswith("switchport access vlan"):
            rollback.append("no switchport access vlan")
        elif command.startswith("description "):
            rollback.append("no description")

    rollback.append(CONFIG_EXIT)
    return rollback


def classify_risk(commands: list[str], device: Device) -> str:
    if device.role in {"isp", "firewall"}:
        return "high"
    if device.role in CRITICAL_ROLES:
        return "medium"
    return "low"


def build_warnings(commands: list[str], device: Device) -> list[str]:
    warnings: list[str] = []
    if device.role in CRITICAL_ROLES:
        warnings.append(
            f"{device.hostname} is a {device.role} device; a mistake here affects "
            "downstream segments."
        )
    if any(command.startswith("vlan ") for command in commands):
        warnings.append(
            "The VLAN will be created if it does not already exist on the device."
        )
    if any(command.startswith("switchport access vlan") for command in commands):
        warnings.append(
            "Moving an access port between VLANs will briefly interrupt any host "
            "connected to it."
        )
    if device.status != "online":
        warnings.append(
            f"{device.hostname} is currently {device.status}; apply may fail."
        )
    warnings.append(
        "A running-config backup is taken automatically before the change is applied."
    )
    return warnings


# -- public API -----------------------------------------------------------


def create_preview(
    user_id: int | None,
    device_id: int,
    commands: list[str],
    verification_commands: list[str] | None = None,
    description: str | None = None,
    source: str = "api",
) -> ChangeRequest:
    """Validate a change and store it as pending_approval. Never touches SSH."""
    device = device_service.get_device(device_id)

    canonical = _wrap(validate_commands(commands, device))

    if verification_commands:
        verification = _validate_verification(verification_commands, device)
    else:
        verification = derive_verification_commands(canonical, device)

    change = ChangeRequest(
        device_id=device.id,
        requested_by_id=user_id,
        description=description,
        commands=canonical,
        verification_commands=verification,
        rollback_commands=derive_rollback_commands(canonical),
        warnings=build_warnings(canonical, device),
        risk_level=classify_risk(canonical, device),
        status="pending_approval",
        source=source,
    )
    db.session.add(change)
    db.session.commit()
    return change


def _validate_verification(commands: list[str], device: Device) -> list[str]:
    """Verification commands must themselves pass the read-only policy."""
    from ..commands.policy import default_policy

    verified: list[str] = []
    for raw in commands:
        decision = default_policy.evaluate(raw, device.role)
        if not decision.allowed:
            raise PolicyViolationError(
                f"Verification command rejected: {decision.reason}",
                {"command": _normalise(raw)},
            )
        verified.append(decision.normalized_command)
    return verified


def get_change(change_id: int) -> ChangeRequest:
    change = db.session.get(ChangeRequest, change_id)
    if change is None:
        raise NotFoundError(f"Change request {change_id} was not found.")
    return change


def list_changes(
    device_id: int | None = None, status: str | None = None, limit: int = 100
) -> list[ChangeRequest]:
    query = db.session.query(ChangeRequest)
    if device_id:
        query = query.filter(ChangeRequest.device_id == device_id)
    if status:
        query = query.filter(ChangeRequest.status == status)
    return (
        query.order_by(ChangeRequest.created_at.desc(), ChangeRequest.id.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
