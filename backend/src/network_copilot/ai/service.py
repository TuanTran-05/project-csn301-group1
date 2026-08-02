"""AI copilot.

Design rules, enforced by tests:

* The model never receives credentials, management IPs or a full running-config.
* The model never executes anything. It returns a structured AIAction which the
  backend validates and freezes into the change workflow.
* A `configure` intent can only ever produce a Preview. Applying stays a manual,
  admin-only step.
* Troubleshooting runs in two phases: propose read-only diagnostics, then explain
  the collected output. A fix is never applied automatically.
"""

import json
import logging
import re

from pydantic import ValidationError as PydanticValidationError

from ..audit.service import record_event, redact_sensitive
from ..auth.model import User
from ..chat import service as chat_service
from ..changes import batch_service
from ..changes.batch_service import BatchOperation
from ..commands import service as command_service
from ..commands.policy import default_policy
from ..devices import service as device_service
from ..devices.model import Device
from ..errors import ForbiddenError, PolicyViolationError, ValidationError
from ..extensions import db
from ..parsers import parse_command_output
from .provider import build_provider
from .schemas import AIAction, AIOperation, build_ai_action_schema

logger = logging.getLogger(__name__)

# Never advertise this to the model: a full config dump is exactly the kind of
# sensitive payload that must not leave the backend.
CONTEXT_EXCLUDED_COMMANDS = {"show running-config"}

SYSTEM_PROMPT = """You are a network operations assistant for a Cisco lab.

Reply with a single JSON object and nothing else. The schema is:
{
  "intent": "monitor" | "configure" | "troubleshoot" | "chat",
  "operations": [{
    "device_hostnames": ["<hostname>", ...] | ["*"],
    "execution_mode": "config" | "exec",
    "commands": ["<command>", ...],
    "verification_commands": ["<read-only command>", ...]
  }],
  "explanation": "<one or two sentences>"
}

Rules:
- Every operation needs at least one target and one command.
- Use "configure" for any Cisco CLI configuration or privileged-EXEC command.
  A configure response only creates a frozen backend preview; it never executes
  commands or bypasses approval.
- Use ["*"] only when the user explicitly requests every device. Never mix "*"
  with explicit hostnames. Split heterogeneous targets into separate operations.
- Select "exec" for privileged-EXEC commands and "config" for configuration
  commands. Configuration commands do not need mode wrappers; the backend adds
  them. Never claim that a proposal was already executed.
- Never emit or decide risk, confirmation, approval, or authorization fields.
  The backend derives and enforces all of them independently.
- For "monitor" and "troubleshoot", return exactly one operation for one explicit
  hostname in "exec" mode. Use only read-only entries from supported_commands.
- Use "monitor" when the user wants to read state. Put the read-only commands in
  "commands" and leave "verification_commands" empty.
- Use "troubleshoot" when the user reports a problem. Put read-only diagnostic
  commands in "commands", not in "verification_commands".
- Use "chat" for messages that are not a request to act on a device:
  greetings, small talk, questions about what you can do, and general or
  theoretical networking knowledge. For "chat", "operations" must be empty
  and "explanation" carries your actual answer, which may be a short
  paragraph rather than a single sentence.
- Never use "chat" to describe, guess at, or invent the live state of any
  device in this lab. Any question about what a device is actually doing
  right now must use "monitor" or "troubleshoot" and run a real command.
- If a question is outside networking entirely, reply with a brief "chat"
  answer saying it is outside what you cover. Never invent an answer.
- For "configure", "verification_commands" contains only read-only commands that
  prove the change landed. It may be empty so the backend can derive verification.
- A device's "status" in context is only the last background health check and
  can be stale or wrong. It is never a reason to skip commands: always propose
  the commands for "monitor"/"troubleshoot" requests regardless of the listed
  status. A device that is actually unreachable will fail naturally when the
  command runs, and that failure is reported back to the user.
- If you cannot form a valid proposal, return an empty "operations" list and
  explain why in "explanation".
- The user may write in Vietnamese or English. Reply with JSON either way. For
  "monitor", "configure" and "troubleshoot", keep "explanation" to a single
  short sentence.
"""

EXPLAIN_PROMPT = """You are a network operations assistant for a Cisco lab.

You are given the output of read-only diagnostic commands. Explain in plain
language what the output shows and what the likely cause is. Suggest what an
engineer should check next. Do not claim to have changed anything: you cannot.
"""


class AIService:
    def __init__(self, provider=None):
        self._provider = provider

    @property
    def provider(self):
        if self._provider is None:
            self._provider = build_provider()
        return self._provider

    # -- context ----------------------------------------------------------
    def build_context(self) -> dict:
        """Safe lab context; per-session conversation is added by interpret()."""
        devices = db.session.query(Device).order_by(Device.hostname).all()
        commands = sorted(
            {
                rule.name
                for rule in default_policy.rules
                if rule.name not in CONTEXT_EXCLUDED_COMMANDS
            }
        )
        return {
            "devices": [
                {
                    "hostname": device.hostname,
                    "role": device.role,
                    "device_type": device.device_type,
                    "status": device.status,
                }
                for device in devices
            ],
            "supported_commands": commands,
        }

    # -- conversation history ---------------------------------------------
    # How many recent turns the model sees, and how many rows to fetch to
    # find them: the fetch is wider than the window because system messages
    # and the duplicate of the current turn are filtered out below.
    HISTORY_LIMIT = 10
    _HISTORY_FETCH_LIMIT = 40

    @classmethod
    def _recent_history(
        cls, session_id: int | None, current_message: str
    ) -> list[dict]:
        """Recent turns of this chat session, for conversational follow-ups.

        Only role and content are returned. A message's payload carries raw
        command output, which can contain configuration detail, and this
        module's standing rule is that the model never receives credentials,
        management IPs or a running-config.
        """
        if session_id is None:
            return []

        rows = chat_service.list_messages(
            session_id=session_id, limit=cls._HISTORY_FETCH_LIMIT
        )
        turns = [
            {"role": row.role, "content": row.content}
            for row in rows
            if row.role in ("user", "assistant")
        ]

        # ai/routes.py records the incoming message before calling handle(),
        # so it is already the newest row here.
        if (
            turns
            and turns[-1]["role"] == "user"
            and turns[-1]["content"] == current_message
        ):
            turns.pop()

        return turns[-cls.HISTORY_LIMIT :]

    # -- interpret --------------------------------------------------------
    @staticmethod
    def _extract_json(raw: str) -> dict:
        """Pull the first complete JSON object out of a model response.

        Models wrap the object in prose or code fences, and sometimes emit two
        objects back to back, even when the API is asked for JSON. Only the
        first one is honoured: silently merging several proposed actions would
        mean running commands the user never saw.
        """
        text = (raw or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if fenced is not None:
            text = fenced.group(1).strip()

        start = text.find("{")
        if start == -1:
            raise ValidationError("The AI response did not contain a JSON object.")

        try:
            # raw_decode stops at the end of the first value, so trailing prose
            # or a second object does not invalidate the response.
            payload, _end = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise ValidationError("The AI response was not valid JSON.") from exc

        if not isinstance(payload, dict):
            raise ValidationError("The AI response was not a JSON object.")
        return payload

    def interpret(
        self, message: str, user_id: int | None, session_id: int | None = None
    ) -> AIAction:
        """Ask the model for a structured action. No side effects."""
        context = self.build_context()
        context["conversation"] = self._recent_history(session_id, message)
        schema = build_ai_action_schema(
            device["hostname"] for device in context["devices"]
        )

        # Models occasionally emit malformed JSON (a repeated fragment, a
        # truncated string). That is transient, so retry once. A well-formed
        # refusal below is a real answer and is never retried.
        payload = None
        for attempt in range(2):
            raw = self.provider.complete(
                SYSTEM_PROMPT, message, context, schema=schema
            )
            try:
                payload = self._extract_json(raw)
                break
            except ValidationError:
                if attempt:
                    raise
                logger.warning("AI returned unparseable output; retrying once.")

        # A well-formed empty operation list is a deliberate refusal, not a
        # transient schema fault. Surface the model's explanation and do not
        # retry a real answer. "chat" is the exception: an empty list is its
        # expected shape, and the explanation is the answer itself.
        if (
            payload.get("intent") != "chat"
            and isinstance(payload.get("operations"), list)
            and not payload["operations"]
        ):
            raise ValidationError(
                payload.get("explanation")
                or "The AI could not form a valid proposal for that request."
            )

        try:
            return AIAction(**payload)
        except PydanticValidationError as exc:
            details: dict[str, list[str]] = {}
            for error in exc.errors():
                field = ".".join(str(part) for part in error["loc"]) or "_root"
                details.setdefault(field, []).append(error["msg"])
            raise ValidationError(
                "The AI returned an action that does not match the required schema.",
                details,
            ) from exc

    # -- guards -----------------------------------------------------------
    def _block(
        self,
        reason: str,
        action: AIAction,
        operation: AIOperation,
        device: Device,
        user_id: int | None,
    ) -> None:
        record_event(
            action="ai.command_blocked",
            result="blocked",
            user_id=user_id,
            device_id=device.id,
            message=reason,
            details={
                "intent": action.intent,
                "commands": operation.commands,
                "hostname": device.hostname,
            },
        )
        raise PolicyViolationError(reason, {"commands": operation.commands})

    def _guard_readonly(
        self,
        action: AIAction,
        operation: AIOperation,
        device: Device,
        user_id: int | None,
    ) -> None:
        for command in operation.commands:
            decision = default_policy.evaluate(command, device.role)
            if not decision.allowed:
                self._block(decision.reason, action, operation, device, user_id)

    @staticmethod
    def _readonly_operation(action: AIAction) -> AIOperation:
        if len(action.operations) != 1:
            raise ValidationError(
                "Monitor and troubleshoot require exactly one operation."
            )
        operation = action.operations[0]
        if (
            len(operation.device_hostnames) != 1
            or operation.device_hostnames == ["*"]
        ):
            raise ValidationError(
                "Monitor and troubleshoot require one explicit hostname."
            )
        if operation.execution_mode != "exec":
            raise ValidationError(
                "Monitor and troubleshoot operations must use EXEC mode."
            )
        return operation

    @staticmethod
    def _require_admin(user_id: int | None) -> None:
        user = db.session.get(User, user_id) if user_id is not None else None
        if user is None or user.role != "ADMIN":
            raise ForbiddenError(
                "Only an ADMIN may create a configuration change. "
                "Ask an administrator to review this request."
            )

    # -- intent handlers --------------------------------------------------
    def _run_readonly(
        self, operation: AIOperation, device: Device, user_id: int | None
    ) -> list[dict]:
        results = []
        for command in operation.commands:
            execution = command_service.execute_readonly(
                device_id=device.id,
                command=command,
                user_id=user_id,
                source="ai",
            )
            results.append(
                {
                    "command": execution.command,
                    "status": execution.status,
                    "output": execution.output,
                    "parsed": parse_command_output(execution.command, execution.output)
                    or [],
                    "duration_ms": execution.duration_ms,
                }
            )
        return results

    def _handle_monitor(
        self,
        action: AIAction,
        operation: AIOperation,
        device: Device,
        user_id: int | None,
    ) -> dict:
        return {
            "intent": "monitor",
            "device": device.hostname,
            "explanation": action.explanation,
            "results": self._run_readonly(operation, device, user_id),
            "requires_approval": False,
        }

    def _handle_troubleshoot(
        self,
        action: AIAction,
        operation: AIOperation,
        device: Device,
        user_id: int | None,
    ) -> dict:
        results = self._run_readonly(operation, device, user_id)

        # Phase 2: hand the collected output back to the model for analysis.
        # Outputs are redacted first in case a device echoed a secret.
        analysis_context = {
            "device": {
                "hostname": device.hostname,
                "role": device.role,
                "device_type": device.device_type,
                "status": device.status,
            },
            "diagnostics": redact_sensitive(
                [
                    {"command": item["command"], "output": item["output"]}
                    for item in results
                ]
            ),
        }
        try:
            analysis = self.provider.explain(
                EXPLAIN_PROMPT, action.explanation, analysis_context
            )
        except Exception:  # pragma: no cover - provider failure must not 500
            logger.exception("AI analysis phase failed.")
            analysis = (
                "Diagnostics were collected but the analysis step failed. "
                "Review the raw output below."
            )

        return {
            "intent": "troubleshoot",
            "device": device.hostname,
            "explanation": action.explanation,
            "results": results,
            "analysis": analysis,
            "requires_approval": False,
        }

    def _handle_configure(self, action: AIAction, user_id: int | None) -> dict:
        operations = [
            BatchOperation(
                device_hostnames=list(operation.device_hostnames),
                execution_mode=operation.execution_mode,
                commands=list(operation.commands),
                verification_commands=list(operation.verification_commands),
            )
            for operation in action.operations
        ]
        batch = batch_service.create_batch_preview(
            user_id=user_id,
            operations=operations,
            description=action.explanation[:255] if action.explanation else None,
            source="ai",
        )
        record_event(
            action="ai.action",
            result="success",
            user_id=user_id,
            message=action.explanation,
            details={
                "intent": action.intent,
                "batch_id": batch.id,
                "operation_count": len(operations),
            },
        )
        return {
            "intent": "configure",
            "explanation": action.explanation,
            "batch": batch.to_dict(),
            "requires_approval": True,
        }

    # -- entry point ------------------------------------------------------
    def handle(
        self, message: str, user_id: int | None, session_id: int | None = None
    ) -> dict:
        action = self.interpret(message, user_id, session_id=session_id)

        # A conversational turn touches nothing: no device is resolved, no
        # policy is evaluated, no SSH session is opened, and no audit row is
        # written (the transcript in chat_messages is the record).
        if action.intent == "chat":
            return {
                "intent": "chat",
                "explanation": action.explanation,
                "requires_approval": False,
            }

        if action.intent == "configure":
            self._require_admin(user_id)
            return self._handle_configure(action, user_id)

        operation = self._readonly_operation(action)
        device = device_service.get_device_by_hostname(operation.device_hostnames[0])
        self._guard_readonly(action, operation, device, user_id)

        record_event(
            action="ai.action",
            result="success",
            user_id=user_id,
            device_id=device.id,
            message=action.explanation,
            details={"intent": action.intent, "commands": operation.commands},
        )

        if action.intent == "monitor":
            return self._handle_monitor(action, operation, device, user_id)
        return self._handle_troubleshoot(action, operation, device, user_id)
