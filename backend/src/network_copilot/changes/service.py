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
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    PolicyViolationError,
    ValidationError,
)
from ..extensions import db
from ..auth.model import User
from ..groups.model import Group
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
from ..governance import workflow

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


_TRUNK_MODE = re.compile(r"^switchport\s+mode\s+trunk$", re.I)
_TRUNK_ENCAPSULATION = re.compile(r"^switchport\s+trunk\s+encapsulation\s+", re.I)
_INTERFACE_BLOCK = re.compile(r"^interface\s+\S+", re.I)


def _with_trunk_encapsulation(commands: list[str]) -> list[str]:
    """Set dot1q before "switchport mode trunk" where the block does not.

    PNETLab's Cisco switch images (IOSv-L2, IOL-L2) also speak ISL, so their
    ports start at "negotiate" and refuse trunk mode with 'Command rejected:
    trunk encapsulation is "Auto"' until an encapsulation is set.
    """
    result: list[str] = []
    encapsulation_set = False
    for command in commands:
        text = command.strip()
        if _INTERFACE_BLOCK.match(text):
            encapsulation_set = False
        elif _TRUNK_ENCAPSULATION.match(text):
            encapsulation_set = True
        elif _TRUNK_MODE.match(text) and not encapsulation_set:
            result.append("switchport trunk encapsulation dot1q")
            encapsulation_set = True
        result.append(command)
    return result


_IDENTITY_CHANGE = re.compile(r"^(?:hostname|ip\s+domain[-\s]name)\s+\S+", re.I)
_SSH_KEYPAIR_PINNED = re.compile(r"(?m)^ip ssh rsa keypair-name\s+\S+")


def _with_pinned_ssh_key(device: Device, commands: list[str]) -> list[str]:
    """Keep SSH alive across a hostname or domain-name change.

    An RSA key generated without a label is named hostname.domain, and IOS
    disables SSH the moment either changes; the session the change runs over
    is cut ("Socket is closed") and the device is unreachable until someone
    fixes it on the console. When the device's newest backup shows SSH is not
    yet pinned to a named key, generate one under the bootstrap's label and
    pin SSH to it before anything is renamed.
    """
    if not any(_IDENTITY_CHANGE.match(command.strip()) for command in commands):
        return commands
    if any(command.strip().lower().startswith("ip ssh rsa keypair-name") for command in commands):
        return commands
    from ..backups.model import ConfigBackup
    from ..topology.bootstrap import SSH_KEY_LABEL

    backup = (
        db.session.query(ConfigBackup)
        .filter_by(device_id=device.id)
        .order_by(ConfigBackup.id.desc())
        .first()
    )
    if backup is not None and _SSH_KEYPAIR_PINNED.search(backup.running_config or ""):
        return commands
    return [
        f"crypto key generate rsa general-keys label {SSH_KEY_LABEL} modulus 2048",
        f"ip ssh rsa keypair-name {SSH_KEY_LABEL}",
        *commands,
    ]


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


# -- Palo Alto PAN-OS ------------------------------------------------------

PANOS_CONFIG_ENTER = "configure"
PANOS_COMMIT = "commit"
_PANOS_LINE = re.compile(r"^(?:set|delete|rename|move)\s+\S", re.I)


def _panos_body(commands: list[str]) -> list[str]:
    from ..ssh.panos import panos_body

    body = panos_body(commands)
    if not body:
        raise ValidationError("The change contains no PAN-OS configuration lines.")
    bad = next((line for line in body if not _PANOS_LINE.match(line)), None)
    if bad is not None:
        raise ValidationError(
            "PAN-OS changes are configure-mode lines starting with set, delete, "
            "rename or move; configure and commit are added by the backend.",
            {"command": bad},
        )
    return body


def derive_panos_rollback(body: list[str]) -> list[str]:
    """Best-effort inverse for PAN-OS: a set is undone by deleting that path.

    A delete cannot be inverted from the command alone; the pre-change backup
    (``show config running``) holds what was there.
    """
    rollback = [PANOS_CONFIG_ENTER]
    for line in body:
        verb, _, rest = line.partition(" ")
        if verb.lower() == "set":
            rollback.append(f"delete {rest}")
        else:
            rollback.append(f"# restore what '{line}' removed or moved from the pre-change backup")
    rollback.append(PANOS_COMMIT)
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
    if device.device_type == "paloalto_panos":
        warnings.append(
            "PAN-OS: the lines are entered in configure mode and made live by commit. "
            "If a line is rejected or the commit fails, the candidate configuration "
            "is reverted and nothing is applied."
        )
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
    group_id: int | None = None,
    assignment_id: int | None = None,
    ai_management_approved: bool = False,
) -> ChangeRequest:
    """Validate and build a ChangeRequest. Does not add/commit it or audit."""
    device_service.ensure_deployable(device)
    panos = device.device_type == "paloalto_panos"
    if panos:
        if execution_mode != "config":
            raise ValidationError(
                "PAN-OS changes run in configure mode and end with commit; "
                "EXEC mode is not supported."
            )
        commands = _panos_body(commands)
    body, requires_confirmation = validate_commands(commands, device)
    if not panos:
        validate_execution_mode(body, execution_mode)
    if device.device_type == "cisco_ios" and device.environment == "pnetlab" and execution_mode == "config":
        body = _with_trunk_encapsulation(body)
        # PNETLab nodes got their SSH key from the console bootstrap, which
        # before SSH_KEY_LABEL generated it unlabelled.
        body = _with_pinned_ssh_key(device, body)

    if execution_mode == "exec" and any(_is_wrapper(command) for command in body):
        raise ValidationError(
            "EXEC mode commands run standalone; configuration wrappers such as "
            "'configure terminal' are not permitted."
        )

    if panos:
        canonical = [PANOS_CONFIG_ENTER, *body, PANOS_COMMIT]
    else:
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
        rollback_commands=derive_panos_rollback(body) if panos else derive_rollback_commands(canonical),
        warnings=build_warnings(canonical, device, requires_confirmation),
        capability_tier=assessment.capability_tier,
        verification_level=assessment.verification_level,
        operation_families=list(assessment.operation_families),
        operation_expectations=[item.to_dict() for item in assessment.expectations],
        verification_plan=plan,
        rollback_guidance=build_rollback_guidance(assessment, canonical),
        risk_level=classify_risk(canonical, device, requires_confirmation),
        requires_confirmation=requires_confirmation,
        ai_management_approved=ai_management_approved,
        status="pending_approval",
        source=source,
    )

    if replaced:
        change.warnings = list(change.warnings or []) + [
            "The requested verification commands were replaced by the backend's frozen semantic plan."
        ]
    change.assignment_id = assignment_id
    workflow.bind(change, user_id, device, group_id)
    return change


def _refresh_baseline(device: Device, change: ChangeRequest, client):
    """Store the configuration the write left behind as the device's baseline.

    The next preview freezes the hash of the newest backup, so without this
    it would hold the pre-change config and its apply would refuse with
    "Running configuration changed since preview". A failed read only costs
    that convenience; the change itself already happened.
    """
    from ..backups.service import capture_backup
    from ..ssh.exceptions import SSHError
    from ..ssh.messages import friendly_error

    try:
        return capture_backup(device, change_request_id=change.id, reason="post_change", client=client)
    except SSHError as exc:
        logger.warning(
            "Post-change backup of %s failed: %s", device.hostname, friendly_error(exc, device.hostname)
        )
        return None


def create_preview(
    user_id: int | None,
    device_id: int,
    commands: list[str],
    verification_commands: list[str] | None = None,
    description: str | None = None,
    source: str = "api",
    execution_mode: str = "config",
    project_id: int | None = None,
    group_id: int | None = None,
    assignment_id: int | None = None,
) -> ChangeRequest:
    """Validate a change and store it as pending_approval. Never touches SSH.

    ``project_id`` confines the target to one project; routes always pass it.
    """
    device = device_service.get_device(device_id, project_id)

    change = prepare_change(
        user_id,
        device,
        commands,
        verification_commands=verification_commands,
        description=description,
        source=source,
        execution_mode=execution_mode,
        group_id=group_id,
        assignment_id=assignment_id,
    )
    from ..history_extension.pending_service import capture_before, record_after
    history_before = capture_before(user_id)
    db.session.add(change)
    db.session.flush()
    workflow.notify_change(change, "change.pending")
    db.session.commit()
    record_after(history_before, "CREATE_PENDING", change)

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
        # The description is free text and may quote a secret; it stays on the
        # access-controlled change record that change_id points to.
        message="Change previewed.",
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


def get_change(change_id: int, project_id: int | None = None) -> ChangeRequest:
    change = db.session.get(ChangeRequest, change_id)
    if change is not None and project_id is not None:
        # Another project's change is indistinguishable from a missing one.
        if change.device is None or change.device.project_id != project_id:
            change = None
    if change is None:
        raise NotFoundError(f"Change request {change_id} was not found.")
    from ..governance.visibility import check_change
    check_change(change)
    return change


def _role_name(user: User) -> str:
    return user.role.value if hasattr(user.role, "value") else str(user.role)


def _require_actor(user_id: int | None) -> User:
    actor = db.session.get(User, user_id) if user_id is not None else None
    if actor is None or not actor.is_active:
        raise ForbiddenError("An active authenticated user is required.")
    return actor


def _groups_for_device(user: User, device: Device, role: str) -> list[Group]:
    memberships = (
        user.teaching_groups if role == "TEACHER" else user.student_groups
    )
    device_group_ids = {group.id for group in device.groups}
    return [group for group in memberships if group.id in device_group_ids]


def _authorize_device_access(actor: User, device: Device) -> None:
    role = _role_name(actor)
    if role == "ADMIN":
        return
    if role not in {"TEACHER", "STUDENT"}:
        raise ForbiddenError("This role cannot manage configuration changes.")
    if not _groups_for_device(actor, device, role):
        raise ForbiddenError(
            "The device is not assigned to a group managed by this user."
        )


def list_changes(
    device_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    standalone_only: bool = False,
    project_id: int | None = None,
) -> list[ChangeRequest]:
    query = db.session.query(ChangeRequest)
    if project_id is not None:
        query = query.join(Device, Device.id == ChangeRequest.device_id).filter(
            Device.project_id == project_id
        )
    if device_id:
        query = query.filter(ChangeRequest.device_id == device_id)
    if status:
        query = query.filter(ChangeRequest.status == status)
    if standalone_only:
        query = query.filter(ChangeRequest.batch_id.is_(None))
    from ..governance.visibility import changes as visible_changes
    return visible_changes(
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
    actor = _require_actor(user_id)
    workflow.approve_allowed(change, user_id)
    if _role_name(actor) == "TEACHER":
        _authorize_device_access(actor, device_service.get_device(change.device_id))
    elif _role_name(actor) != "ADMIN":
        raise ForbiddenError("Only an Admin or the assigned Teacher can approve changes.")
    if change.status != "pending_approval":
        raise InvalidStateError(
            f"Only a change in 'pending_approval' can be approved; "
            f"change {change_id} is '{change.status}'."
        )
    change.status = "approved"
    change.approved_by_id = user_id
    change.approved_at = _now()
    workflow.notify_change(change, "change.approved")
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
    from ..history_extension.pending_service import capture_before, record_after
    history_before = capture_before(user_id)
    change.status = "cancelled"
    db.session.commit()
    record_after(history_before, "CANCEL_PENDING", change)
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
    workflow.notify_change(change, "change.failed")
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

    actor = _require_actor(user_id)
    role = _role_name(actor)
    device = device_service.get_device(change.device_id)

    from ..governance.access import require
    require(actor, "config.deploy", change.group_id, device)
    if role == "STUDENT" and change.status != "approved":
        if change.requested_by_id != actor.id:
            raise ForbiddenError("Students may only apply their own changes.")
        groups = _groups_for_device(actor, device, role)
        if not groups:
            raise ForbiddenError(
                "The device is not assigned to one of your groups."
            )
        if change.status != "pending_approval":
            raise ForbiddenError(
                "Students may apply only their own pending changes through "
                "Smart Approval."
            )

        from ..commands.policy import analyze_risk

        risk = analyze_risk(change.commands or [], device.role)
        auto_approve_groups = [group for group in groups if group.is_auto_approve and group.id == change.group_id]
        if not auto_approve_groups or workflow.risk(change.commands, device) != "low" or risk != "LOW" or change.requires_confirmation:
            raise ForbiddenError(
                "Teacher approval is required because Smart Approval is disabled "
                "for this group or the change contains a high-risk command."
            )

        change.status = "approved"
        change.approved_by_id = None
        change.approved_at = _now()
        change.approval_digest = workflow.digest(change)
        db.session.commit()
        record_event(
            action="change.approve",
            result="success",
            user_id=actor.id,
            device_id=change.device_id,
            message="Automatically approved by Smart Approval.",
            details={
                "change_id": change.id,
                "risk_level": risk,
                "approval_mode": "smart",
            },
        )
    elif role == "TEACHER":
        _authorize_device_access(actor, device)
    elif role not in {"ADMIN", "STUDENT"}:
        raise ForbiddenError("This role cannot apply configuration changes.")

    if change.status != "approved":
        raise InvalidStateError(
            f"Only an approved change can be applied; change {change_id} is "
            f"'{change.status}'."
        )

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
    if change.status != "approved":
        raise InvalidStateError("Only an approved change can be applied.")
    if change.batch_id is not None:
        parent = db.session.get(ChangeBatch, change.batch_id)
        if parent is None or parent.status != "running":
            raise InvalidStateError("A batch child requires its parent to be running.")
    workflow.deployment_allowed(change, user_id)
    workflow.claim(change, user_id)
    try:
        return _execute_locked_change(change, user_id)
    except Exception:
        db.session.rollback()
        _fail(change, "Deployment interrupted; inspect device state before creating another preview.", user_id)
        raise
    finally:
        workflow.release(change)


def _execute_locked_change(change: ChangeRequest, user_id: int | None) -> ChangeRequest:
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

    if change.status != "running":
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

    device = device_service.get_device(
        change.device_id, ai_management_change=change
    )

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
    if change.baseline_hash and workflow.config_hash(backup.running_config) != change.baseline_hash:
        return _fail(change, "Running configuration changed since preview; preview and approve again.", user_id)
    # Re-read permissions after backup I/O; revocation must win before any write.
    db.session.expire_all()
    workflow.deployment_allowed(change, user_id)
    device.baseline_revision += 1
    db.session.commit()
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
    post_backup = _refresh_baseline(device, change, client)
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
    workflow.notify_change(change, "change.success")
    db.session.commit()
    device_service.set_device_status(device, "online")
    logger.info("Change %s applied and verified on %s.", change.id, device.hostname)
    # Undo/redo step for the Pending column. Never raises.
    from ..checkpoints.service import on_change_applied
    on_change_applied(change, device, client, post_backup)
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
