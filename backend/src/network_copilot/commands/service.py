"""Read-only command execution.

Routes never talk to Paramiko directly: they call into this module, which runs
the policy engine first and records the outcome either way.
"""

import logging

from ..audit.service import record_event
from ..devices import service as device_service
from ..errors import PolicyViolationError, ValidationError
from ..extensions import db
from ..ssh.client import build_client_for_device
from ..ssh.exceptions import SSHError
from .model import CommandExecution
from .policy import CommandDecision, default_policy

logger = logging.getLogger(__name__)


def _record(
    device_id: int | None,
    user_id: int | None,
    command: str,
    status: str,
    output: str | None = None,
    reason: str | None = None,
    duration_ms: int = 0,
    source: str = "api",
) -> CommandExecution:
    execution = CommandExecution(
        device_id=device_id,
        user_id=user_id,
        command=command,
        output=output,
        status=status,
        reason=reason,
        duration_ms=duration_ms,
        source=source,
    )
    db.session.add(execution)
    db.session.commit()
    return execution


def evaluate(command: str, device_role: str) -> CommandDecision:
    return default_policy.evaluate(command, device_role)


def execute_readonly(
    device_id: int,
    command: str,
    user_id: int | None = None,
    source: str = "api",
) -> CommandExecution:
    """Run a single read-only command after the policy engine approves it."""
    if not isinstance(command, str) or not command.strip():
        raise ValidationError("A non-empty 'command' is required.")

    # Raises NotFoundError when the device does not exist.
    device = device_service.get_device(device_id)

    decision = default_policy.evaluate(command, device.role)
    if not decision.allowed:
        _record(
            device_id=device.id,
            user_id=user_id,
            command=decision.normalized_command or command,
            status="blocked",
            reason=decision.reason,
            source=source,
        )
        logger.warning(
            "Blocked command on %s: %s", device.hostname, decision.reason
        )
        record_event(
            action="command.blocked",
            result="blocked",
            user_id=user_id,
            device_id=device.id,
            message=decision.reason,
            details={
                "command": decision.normalized_command or command,
                "source": source,
            },
        )
        raise PolicyViolationError(
            decision.reason,
            {"command": decision.normalized_command, "device": device.hostname},
        )

    try:
        client = build_client_for_device(device)
        result = client.run_show(decision.normalized_command)
    except SSHError as exc:
        _record(
            device_id=device.id,
            user_id=user_id,
            command=decision.normalized_command,
            status="failed",
            reason=exc.message,
            source=source,
        )
        device_service.set_device_status(device, "offline")
        record_event(
            action="command.readonly",
            result="failure",
            user_id=user_id,
            device_id=device.id,
            message=exc.message,
            details={"command": decision.normalized_command, "source": source},
        )
        raise

    device_service.set_device_status(device, "online")
    execution = _record(
        device_id=device.id,
        user_id=user_id,
        command=decision.normalized_command,
        status="success",
        output=result.output,
        duration_ms=result.duration_ms,
        source=source,
    )
    record_event(
        action="command.readonly",
        result="success",
        user_id=user_id,
        device_id=device.id,
        details={
            "command": decision.normalized_command,
            "source": source,
            "duration_ms": result.duration_ms,
        },
    )
    return execution


def list_history(
    device_id: int | None = None,
    user_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[CommandExecution]:
    query = db.session.query(CommandExecution)
    if device_id:
        query = query.filter(CommandExecution.device_id == device_id)
    if user_id:
        query = query.filter(CommandExecution.user_id == user_id)
    if status:
        query = query.filter(CommandExecution.status == status)
    return (
        query.order_by(CommandExecution.created_at.desc(), CommandExecution.id.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
