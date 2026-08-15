"""Configuration change workflow: Preview -> Approve -> Apply -> Verify.

The AI or an operator may propose any configuration command - a Preview is a
proposal, not an action, and still needs a human to Approve and Apply it.
Two things are not just risky-but-allowed, they are refused outright:

  * chaining a second command via a shell metacharacter (";", "|", "&", ...)
  * an empty command list

Everything else is accepted, but a command matching an inherently dangerous
pattern (write erase, reload, shutdown, no router ospf, ...) or touching a
reserved VLAN sets ``requires_confirmation`` on the change instead of being
blocked. Apply then requires the caller to type the device hostname back
(see ``apply()``), a stronger check than the single Approve/Apply click every
other change already goes through.
"""

import logging
import re
from datetime import datetime, timezone

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
from .capabilities import assess_change
from .verification import (
    build_verification_plan,
    flatten_verification_commands,
    is_sensitive_verification_command,
    run_verification,
    serialize_verification_evidence,
)
from .model import ChangeBatch, ChangeRequest
from .rollback import build_rollback_guidance

logger = logging.getLogger(__name__)

# VLANs that must never be created, renamed, deleted, or assigned to a port
# without confirmation.
SYSTEM_VLANS = {1, 1002, 1003, 1004, 1005}

CONFIG_ENTER = "configure terminal"
CONFIG_EXIT = "end"
WRAPPERS = {CONFIG_ENTER, CONFIG_EXIT, "exit"}

# Commands that only make sense as privileged EXEC operations - they must be
# run outside a "configure terminal" block. Config mode refuses them so an
# operator (or the AI) is forced to say "exec" explicitly for anything that
# writes, reloads, or otherwise acts immediately instead of staging config.
EXEC_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"^write\b", r"^copy\b", r"^reload\b", r"^erase\b",
        r"^delete\b", r"^format\b", r"^clear\b", r"^debug\b",
        r"^undebug\b", r"^show\b", r"^ping\b", r"^traceroute\b",
    )
)

CRITICAL_ROLES = {"core", "distribution"}

_SYSTEM_VLAN_PATTERNS = (
    re.compile(r"^(no\s+)?vlan\s+(?P<vlan_id>\d{1,4})$", re.I),
    re.compile(r"^switchport\s+access\s+vlan\s+(?P<vlan_id>\d{1,4})$", re.I),
)


def _is_wrapper(command: str) -> bool:
    """True if ``command`` is a config-mode wrapper, compared case-insensitively."""
    return command.strip().lower() in WRAPPERS


def _normalise(command: str) -> str:
    return re.sub(r"\s+", " ", str(command).strip())


def _dangerous_reason(command: str) -> str | None:
    """Return why a command is inherently dangerous, or None if it isn't.

    Reuses the same allowlist-of-danger the read-only policy engine already
    uses, so "dangerous" means one thing across the whole backend rather than
    two subtly different lists that could drift apart. Matching is
    case-insensitive ("WRITE MEMORY" is just as dangerous as "write memory"),
    but the stored command keeps its original casing - only the match is
    case-insensitive.
    """
    from ..commands.policy import DANGEROUS_PATTERNS

    for pattern, explanation in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.I):
            return explanation
    return None


def _touches_system_vlan(command: str) -> int | None:
    """Return the VLAN id if this command creates, deletes, renames, or
    assigns a port to a reserved VLAN, else None."""
    for pattern in _SYSTEM_VLAN_PATTERNS:
        match = pattern.match(command)
        if match is not None:
            vlan_id = int(match.group("vlan_id"))
            if vlan_id in SYSTEM_VLANS:
                return vlan_id
    return None


def validate_commands(commands: list[str], device: Device) -> tuple[list[str], bool]:
    """Normalise the commands and report whether any of them are dangerous
    enough to require extra confirmation before Apply.

    Returns (canonical_commands, requires_confirmation).
    """
    from ..commands.policy import SHELL_METACHARACTERS

    if not commands or not isinstance(commands, (list, tuple)):
        raise ValidationError("At least one configuration command is required.")

    canonical: list[str] = []
    requires_confirmation = False

    for raw in commands:
        raw_text = str(raw)

        # Check the metacharacters on the original string: normalising
        # collapses a newline into a space, which would let a
        # newline-smuggled second command slip past this check.
        for token in SHELL_METACHARACTERS:
            if token in raw_text:
                raise PolicyViolationError(
                    f"Command contains the forbidden character sequence {token!r}; "
                    "chained commands are not permitted.",
                    {"command": _normalise(raw)},
                )

        command = _normalise(raw)
        if not command:
            continue

        if not _is_wrapper(command):
            if _dangerous_reason(command) is not None:
                requires_confirmation = True
            if _touches_system_vlan(command) is not None:
                requires_confirmation = True
            if re.match(r"^ip\s+access-group\s+\S+\s+(?:in|out)$", command, re.I):
                requires_confirmation = True

        canonical.append(command)

    if not canonical:
        raise ValidationError("At least one configuration command is required.")
    return canonical, requires_confirmation


def validate_execution_mode(commands: list[str], execution_mode: str) -> None:
    if execution_mode not in {"config", "exec"}:
        raise ValidationError("execution_mode must be 'config' or 'exec'.")
    if execution_mode == "config" and any(
        pattern.search(command) for command in commands for pattern in EXEC_COMMAND_PATTERNS
    ):
        raise ValidationError("This command requires EXEC mode; it cannot run in config mode.")


def _wrap(commands: list[str]) -> list[str]:
    """Strip any caller-supplied wrappers and apply exactly one config block."""
    body = [command for command in commands if not _is_wrapper(command)]
    if not body:
        raise ValidationError(
            "The change contains no configuration commands, only mode wrappers."
        )
    return [CONFIG_ENTER, *body, CONFIG_EXIT]


# -- derived preview content ---------------------------------------------

# EXEC-mode commands whose entire effect is "persist the running config to
# startup". A backend-only verifier is derived for exactly these - it is
# never added to commands/policy.py's READ_ONLY_RULES, so it is never
# advertised to the AI provider as a supported command.
_EXEC_WRITE_COMMANDS = (
    ["write"],
    ["write memory"],
    ["copy running-config startup-config"],
)


def derive_verification_commands(
    commands: list[str], device: Device, execution_mode: str = "config"
) -> list[str]:
    """Read-only commands that prove the change landed."""
    from ..commands.policy import default_policy

    if execution_mode == "exec" and commands in _EXEC_WRITE_COMMANDS:
        return ["show startup-config"]

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


def classify_risk(
    commands: list[str], device: Device, requires_confirmation: bool = False
) -> str:
    if requires_confirmation:
        return "high"
    if device.role in {"isp", "firewall"}:
        return "high"
    if device.role in CRITICAL_ROLES:
        return "medium"
    return "low"


def build_warnings(
    commands: list[str], device: Device, requires_confirmation: bool = False
) -> list[str]:
    warnings: list[str] = []
    if requires_confirmation:
        warnings.append(
            "This change contains a command flagged as dangerous. Applying it "
            "will require typing the device hostname to confirm."
        )
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


def prepare_change(
    user_id: int | None,
    device: Device,
    commands: list[str],
    verification_commands: list[str] | None = None,
    description: str | None = None,
    source: str = "api",
    execution_mode: str = "config",
) -> ChangeRequest:
    """Validate and build a ChangeRequest. Does not add/commit it or audit."""
    body, requires_confirmation = validate_commands(commands, device)
    validate_execution_mode(body, execution_mode)

    if execution_mode == "exec" and any(_is_wrapper(command) for command in body):
        raise ValidationError(
            "EXEC mode commands run standalone; configuration wrappers such as "
            "'configure terminal' are not permitted."
        )

    canonical = _wrap(body) if execution_mode == "config" else body
    assessment = assess_change(body, execution_mode, device.device_type)

    backend_save = execution_mode == "exec" and canonical in _EXEC_WRITE_COMMANDS
    requested_verification = [] if backend_save else (_validate_verification(verification_commands, device) if verification_commands else [])
    if backend_save:
        verification = ["show startup-config"]
    else:
        plan = build_verification_plan(assessment, requested_verification, device)
        verification = flatten_verification_commands(plan)
    plan = build_verification_plan(assessment, requested_verification, device)
    replaced = bool(requested_verification) and requested_verification != flatten_verification_commands(plan)

    change = ChangeRequest(
        device_id=device.id,
        requested_by_id=user_id,
        description=description,
        commands=canonical,
        execution_mode=execution_mode,
        target_hostname=device.hostname,
        target_management_ip=device.management_ip,
        target_ssh_port=device.ssh_port,
        target_device_type=device.device_type,
        verification_commands=verification,
        rollback_commands=derive_rollback_commands(canonical),
        warnings=build_warnings(canonical, device, requires_confirmation),
        capability_tier=assessment.capability_tier,
        verification_level=assessment.verification_level,
        operation_families=list(assessment.operation_families),
        operation_expectations=[item.to_dict() for item in assessment.expectations],
        verification_plan=plan,
        rollback_guidance=build_rollback_guidance(assessment, canonical),
        risk_level=classify_risk(canonical, device, requires_confirmation),
        requires_confirmation=requires_confirmation,
        status="pending_approval",
        source=source,
    )

    if replaced:
        change.warnings = list(change.warnings or []) + [
            "The requested verification commands were replaced by the backend's frozen semantic plan."
        ]
    return change


def create_preview(
    user_id: int | None,
    device_id: int,
    commands: list[str],
    verification_commands: list[str] | None = None,
    description: str | None = None,
    source: str = "api",
    execution_mode: str = "config",
) -> ChangeRequest:
    """Validate a change and store it as pending_approval. Never touches SSH."""
    device = device_service.get_device(device_id)

    change = prepare_change(
        user_id,
        device,
        commands,
        verification_commands=verification_commands,
        description=description,
        source=source,
        execution_mode=execution_mode,
    )
    db.session.add(change)
    db.session.commit()

    if any("replaced by the backend" in warning for warning in (change.warnings or [])):
        record_event(
            action="change.verification_overridden",
            result="success",
            user_id=user_id,
            device_id=device.id,
            details={"change_id": change.id, "requested": verification_commands or [], "effective": change.verification_commands or []},
        )

    record_event(
        action="change.preview",
        result="success",
        user_id=user_id,
        device_id=device.id,
        message=description,
        details={
            "change_id": change.id,
            "commands": change.commands,
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
        if is_sensitive_verification_command(raw):
            raise PolicyViolationError(
                "Full configuration verification is backend-only and cannot be requested.",
                {"command": _normalise(raw)},
            )
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
    device_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    standalone_only: bool = False,
) -> list[ChangeRequest]:
    query = db.session.query(ChangeRequest)
    if device_id:
        query = query.filter(ChangeRequest.device_id == device_id)
    if status:
        query = query.filter(ChangeRequest.status == status)
    if standalone_only:
        query = query.filter(ChangeRequest.batch_id.is_(None))
    return (
        query.order_by(ChangeRequest.created_at.desc(), ChangeRequest.id.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- approve / cancel -----------------------------------------------------


def _reject_batch_child(change: ChangeRequest) -> None:
    """Standalone lifecycle calls must never touch a batch's children -
    the batch endpoints own approve/apply/cancel for every child at once."""
    if change.batch_id is not None:
        raise InvalidStateError(
            f"Change {change.id} belongs to batch {change.batch_id}; use the "
            "batch endpoints to approve, apply, or cancel it."
        )


def approve(change_id: int, user_id: int | None) -> ChangeRequest:
    change = get_change(change_id)
    _reject_batch_child(change)
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
    _reject_batch_child(change)
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


def _legacy_run_verification(change: ChangeRequest, client) -> tuple[bool, dict]:
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


def apply(
    change_id: int, user_id: int | None, confirm_hostname: str | None = None
) -> ChangeRequest:
    """Backup, configure, then verify. Order is fixed and never skipped."""
    change = get_change(change_id)
    _reject_batch_child(change)
    if change.status != "approved":
        raise InvalidStateError(
            f"Only an approved change can be applied; change {change_id} is "
            f"'{change.status}'."
        )

    device = device_service.get_device(change.device_id)

    if change.requires_confirmation:
        provided = (confirm_hostname or "").strip()
        if not provided or provided != device.hostname:
            raise ValidationError(
                "This change contains a dangerous command. Confirm by sending "
                f"confirm_hostname equal to the exact device hostname "
                f"('{device.hostname}').",
                {"confirm_hostname_required": device.hostname},
            )

    return _apply_approved_change(change, user_id)


def _apply_approved_change(change: ChangeRequest, user_id: int | None) -> ChangeRequest:
    """Backup, run, then verify an already-authorized change.

    Callers must enforce confirmation themselves before reaching here -
    ``apply()`` does it for a standalone change and
    ``batch_service.apply_batch()`` does it once for the whole batch before
    any child reaches this function. This function assumes authorization
    already happened and goes straight to SSH.
    """
    from ..backups.service import capture_backup
    from ..ssh.client import build_client_for_device
    from ..ssh.exceptions import SSHError
    from ..ssh.messages import friendly_error

    if change.status != "approved":
        raise InvalidStateError(
            f"Only an approved change can be applied; change {change.id} is "
            f"'{change.status}'."
        )
    if change.batch_id is not None:
        parent = db.session.get(ChangeBatch, change.batch_id)
        if parent is None or parent.status != "running":
            raise InvalidStateError(
                "A batch child can only be applied while its batch is "
                f"'running'; batch {change.batch_id} is "
                f"'{parent.status if parent else 'missing'}'."
            )

    device = device_service.get_device(change.device_id)

    if (
        device.hostname != change.target_hostname
        or device.management_ip != change.target_management_ip
        or device.ssh_port != change.target_ssh_port
        or device.device_type != change.target_device_type
    ):
        return _fail(
            change,
            "Device connection identity changed since this change was "
            "previewed; re-run Preview before applying.",
            user_id,
        )

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
        return _fail(
            change,
            f"Pre-change backup failed: {friendly_error(exc, device.hostname)}",
            user_id,
        )

    # 2. Push the configuration (or run the EXEC commands directly).
    try:
        if change.execution_mode == "exec":
            result = client.run_exec(
                list(change.commands or []), allow_confirm=change.requires_confirmation
            )
        else:
            result = client.run_config(list(change.commands or []))
        change.apply_output = result.output
        db.session.commit()
    except SSHError as exc:
        return _fail(
            change,
            f"Applying configuration failed: {friendly_error(exc, device.hostname)}",
            user_id,
        )

    # 3. Verify on the device itself.
    try:
        passed, results = run_verification(change, client)
    except SSHError as exc:
        change.verification_output = None
        return _fail(
            change,
            f"Verification could not run: {friendly_error(exc, device.hostname)}",
            user_id,
        )

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
