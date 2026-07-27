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

import logging

from ..audit.service import record_event
from ..devices import service as device_service
from ..devices.model import Device
from ..errors import (
    InvalidStateError,
    NotFoundError,
    PolicyViolationError,
    ValidationError,
)
from ..extensions import db
from .model import ChangeRequest

logger = logging.getLogger(__name__)

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

    record_event(
        action="change.preview",
        result="success",
        user_id=user_id,
        device_id=device.id,
        message=description,
        details={
            "change_id": change.id,
            "commands": canonical,
            "risk_level": change.risk_level,
            "source": source,
        },
    )
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


# -- approve / cancel -----------------------------------------------------


def approve(change_id: int, user_id: int | None) -> ChangeRequest:
    change = get_change(change_id)
    if change.status != "pending_approval":
        raise InvalidStateError(
            f"Only a change in 'pending_approval' can be approved; "
            f"change {change_id} is '{change.status}'."
        )
    change.status = "approved"
    change.approved_by_id = user_id
    change.approved_at = _now()
    db.session.commit()
    record_event(
        action="change.approve",
        result="success",
        user_id=user_id,
        device_id=change.device_id,
        details={"change_id": change.id, "risk_level": change.risk_level},
    )
    return change


def cancel(change_id: int, user_id: int | None) -> ChangeRequest:
    change = get_change(change_id)
    if change.status not in {"pending_approval", "approved"}:
        raise InvalidStateError(
            f"A change in state '{change.status}' can no longer be cancelled."
        )
    change.status = "cancelled"
    db.session.commit()
    record_event(
        action="change.cancel",
        result="success",
        user_id=user_id,
        device_id=change.device_id,
        details={"change_id": change.id},
    )
    return change


# -- verification ---------------------------------------------------------


def _expected_vlans(commands: list[str]) -> list[dict]:
    """VLAN id/name pairs the change is expected to produce."""
    expected: list[dict] = []
    current: dict | None = None

    for command in commands:
        vlan_match = re.match(r"^vlan (\d+)$", command)
        if vlan_match is not None:
            current = {"vlan_id": int(vlan_match.group(1)), "name": None}
            expected.append(current)
            continue

        name_match = re.match(r"^name (.+)$", command)
        if name_match is not None and current is not None:
            current["name"] = name_match.group(1).strip()
            continue

        access_match = re.match(r"^switchport access vlan (\d+)$", command)
        if access_match is not None:
            vlan_id = int(access_match.group(1))
            if not any(item["vlan_id"] == vlan_id for item in expected):
                expected.append({"vlan_id": vlan_id, "name": None})
            current = None

    return expected


def _verify_vlan_output(output: str, expected: list[dict]) -> tuple[bool, list[str]]:
    from ..parsers import parse_vlan_brief

    rows = {row["vlan_id"]: row for row in parse_vlan_brief(output)}
    details: list[str] = []
    passed = True

    for item in expected:
        row = rows.get(item["vlan_id"])
        if row is None:
            passed = False
            details.append(f"VLAN {item['vlan_id']} is not present on the device.")
            continue
        if item["name"] and row["name"] != item["name"]:
            passed = False
            details.append(
                f"VLAN {item['vlan_id']} is named '{row['name']}', "
                f"expected '{item['name']}'."
            )
            continue
        details.append(
            f"VLAN {item['vlan_id']} ({row['name']}) is present and {row['status']}."
        )

    return passed, details


def _verify_generic(output: str) -> tuple[bool, list[str]]:
    if not output or not output.strip():
        return False, ["The device returned no output."]
    if "% Invalid input" in output or "% Incomplete command" in output:
        return False, ["The device rejected the verification command."]
    return True, ["Command returned output."]


def run_verification(change: ChangeRequest, client) -> tuple[bool, dict]:
    """Run each verification command and judge the result."""
    expected_vlans = _expected_vlans(change.commands or [])
    results: dict[str, dict] = {}
    all_passed = True

    for command in change.verification_commands or []:
        result = client.run_show(command)
        if command == "show vlan brief" and expected_vlans:
            passed, details = _verify_vlan_output(result.output, expected_vlans)
        else:
            passed, details = _verify_generic(result.output)

        results[command] = {
            "passed": passed,
            "output": result.output,
            "details": details,
        }
        all_passed = all_passed and passed

    return all_passed, results


# -- apply ----------------------------------------------------------------


def _fail(change: ChangeRequest, message: str, user_id: int | None = None) -> ChangeRequest:
    change.status = "failed"
    change.error_message = message[:512]
    change.applied_at = _now()
    db.session.commit()
    logger.warning("Change %s failed: %s", change.id, message)
    record_event(
        action="change.apply",
        result="failure",
        user_id=user_id if user_id is not None else change.approved_by_id,
        device_id=change.device_id,
        message=message,
        details={
            "change_id": change.id,
            "rollback_commands": change.rollback_commands or [],
        },
    )
    return change


def apply(change_id: int, user_id: int | None) -> ChangeRequest:
    """Backup, configure, then verify. Order is fixed and never skipped."""
    from ..backups.service import capture_backup
    from ..ssh.client import build_client_for_device
    from ..ssh.exceptions import SSHError

    change = get_change(change_id)
    if change.status != "approved":
        raise InvalidStateError(
            f"Only an approved change can be applied; change {change_id} is "
            f"'{change.status}'."
        )

    device = device_service.get_device(change.device_id)
    change.status = "running"
    db.session.commit()

    try:
        client = build_client_for_device(device)
    except Exception as exc:  # pragma: no cover - missing credentials etc.
        return _fail(change, f"Could not open an SSH session: {exc}", user_id)

    # 1. Backup first. Without it, nothing is configured.
    try:
        backup = capture_backup(device, change_request_id=change.id, client=client)
        change.backup_id = backup.id
        db.session.commit()
    except SSHError as exc:
        return _fail(change, f"Pre-change backup failed: {exc.message}", user_id)

    # 2. Push the configuration.
    try:
        result = client.run_config(list(change.commands or []))
        change.apply_output = result.output
        db.session.commit()
    except SSHError as exc:
        return _fail(change, f"Applying configuration failed: {exc.message}", user_id)

    # 3. Verify on the device itself.
    try:
        passed, results = run_verification(change, client)
    except SSHError as exc:
        change.verification_output = None
        return _fail(change, f"Verification could not run: {exc.message}", user_id)

    change.verification_output = results
    if not passed:
        return _fail(
            change,
            "Verification failed; the device does not reflect the requested change. "
            "Review rollback_commands and apply them manually if required.",
            user_id,
        )

    change.status = "success"
    change.error_message = None
    change.applied_at = _now()
    db.session.commit()
    device_service.set_device_status(device, "online")
    logger.info("Change %s applied and verified on %s.", change.id, device.hostname)
    record_event(
        action="change.apply",
        result="success",
        user_id=user_id,
        device_id=change.device_id,
        details={
            "change_id": change.id,
            "commands": change.commands or [],
            "backup_id": change.backup_id,
            "verification_commands": change.verification_commands or [],
        },
    )
    return change
