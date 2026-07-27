"""AI copilot.

Design rules, enforced by tests:

* The model never receives credentials, management IPs or a full running-config.
* The model never executes anything. It returns a structured AIAction which the
  Command Policy Engine and the change workflow then accept or refuse.
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
from ..changes import service as change_service
from ..commands import service as command_service
from ..commands.policy import default_policy
from ..devices import service as device_service
from ..devices.model import Device
from ..errors import ForbiddenError, PolicyViolationError, ValidationError
from ..extensions import db
from ..parsers import parse_command_output
from .provider import build_provider
from .schemas import AI_ACTION_SCHEMA, AIAction

logger = logging.getLogger(__name__)

# Never advertise this to the model: a full config dump is exactly the kind of
# sensitive payload that must not leave the backend.
CONTEXT_EXCLUDED_COMMANDS = {"show running-config"}

SUPPORTED_ACTIONS = (
    "Create or rename a VLAN (vlan <id> / name <NAME>)",
    "Assign an access port to a VLAN (switchport mode access / "
    "switchport access vlan <id>)",
    "Set an interface description (description <text>)",
)

SYSTEM_PROMPT = """You are a network operations assistant for a Cisco lab.

Reply with a single JSON object and nothing else. The schema is:
{
  "intent": "monitor" | "configure" | "troubleshoot",
  "device_hostname": "<one of the hostnames in the context>",
  "commands": ["<command>", ...],
  "verification_commands": ["<read-only command>", ...],
  "explanation": "<one or two sentences>"
}

Rules:
- "commands" is what will actually be run. It must never be empty.
- "verification_commands" is only meaningful for "configure": read-only commands
  that prove the change landed. Leave it empty for other intents.
- Use "monitor" when the user wants to read state. Put the read-only commands in
  "commands", using only entries from supported_commands.
- Use "configure" only for the changes listed in supported_actions. Put them in
  "commands", wrapped in "configure terminal" ... "end".
- Use "troubleshoot" when the user reports a problem. Put the read-only
  diagnostic commands in "commands" too, not in "verification_commands".
- Never propose commands that erase, reload, format, debug or otherwise disrupt
  a device. They will be rejected.
- If you cannot answer with a supported command, return an empty "commands" list
  and explain why in "explanation".
- The user may write in Vietnamese or English. Reply with JSON either way, and
  keep "explanation" to a single short sentence.
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
        """Everything the model is allowed to know. Nothing more."""
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
            "supported_actions": list(SUPPORTED_ACTIONS),
        }

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

    def interpret(self, message: str, user_id: int | None) -> AIAction:
        """Ask the model for a structured action. No side effects."""
        context = self.build_context()

        # Models occasionally emit malformed JSON (a repeated fragment, a
        # truncated string). That is transient, so retry once. A well-formed
        # refusal below is a real answer and is never retried.
        payload = None
        for attempt in range(2):
            raw = self.provider.complete(
                SYSTEM_PROMPT, message, context, schema=AI_ACTION_SCHEMA
            )
            try:
                payload = self._extract_json(raw)
                break
            except ValidationError:
                if attempt:
                    raise
                logger.warning("AI returned unparseable output; retrying once.")

        # A capable model refuses a dangerous or unsupported request itself and
        # answers with no commands. Surface its reason instead of a schema
        # complaint, which reads like a backend fault.
        if isinstance(payload.get("commands"), list) and not payload["commands"]:
            raise ValidationError(
                payload.get("explanation")
                or "The AI could not map that request to a supported action."
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
        self, reason: str, action: AIAction, device: Device, user_id: int | None
    ) -> None:
        record_event(
            action="ai.command_blocked",
            result="blocked",
            user_id=user_id,
            device_id=device.id,
            message=reason,
            details={
                "intent": action.intent,
                "commands": action.commands,
                "hostname": device.hostname,
            },
        )
        raise PolicyViolationError(reason, {"commands": action.commands})

    def _guard(self, action: AIAction, device: Device, user_id: int | None) -> None:
        """Refuse anything the policy engine or the templates do not allow."""
        if action.intent in {"monitor", "troubleshoot"}:
            for command in action.commands:
                decision = default_policy.evaluate(command, device.role)
                if not decision.allowed:
                    self._block(decision.reason, action, device, user_id)
            return

        try:
            change_service.validate_commands(action.commands, device)
        except (PolicyViolationError, ValidationError) as exc:
            self._block(exc.message, action, device, user_id)

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
        self, action: AIAction, device: Device, user_id: int | None
    ) -> list[dict]:
        results = []
        for command in action.commands:
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
        self, action: AIAction, device: Device, user_id: int | None
    ) -> dict:
        return {
            "intent": "monitor",
            "device": device.hostname,
            "explanation": action.explanation,
            "results": self._run_readonly(action, device, user_id),
            "requires_approval": False,
        }

    def _handle_troubleshoot(
        self, action: AIAction, device: Device, user_id: int | None
    ) -> dict:
        results = self._run_readonly(action, device, user_id)

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

    def _handle_configure(
        self, action: AIAction, device: Device, user_id: int | None
    ) -> dict:
        change = change_service.create_preview(
            user_id=user_id,
            device_id=device.id,
            commands=action.commands,
            verification_commands=action.verification_commands,
            description=action.explanation[:255] if action.explanation else None,
            source="ai",
        )
        return {
            "intent": "configure",
            "device": device.hostname,
            "explanation": action.explanation,
            "change": change.to_dict(),
            "requires_approval": True,
        }

    # -- entry point ------------------------------------------------------
    def handle(self, message: str, user_id: int | None) -> dict:
        action = self.interpret(message, user_id)
        device = device_service.get_device_by_hostname(action.device_hostname)

        if action.intent == "configure":
            self._require_admin(user_id)

        self._guard(action, device, user_id)

        record_event(
            action="ai.action",
            result="success",
            user_id=user_id,
            device_id=device.id,
            message=action.explanation,
            details={"intent": action.intent, "commands": action.commands},
        )

        if action.intent == "monitor":
            return self._handle_monitor(action, device, user_id)
        if action.intent == "troubleshoot":
            return self._handle_troubleshoot(action, device, user_id)
        return self._handle_configure(action, device, user_id)
