from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AIOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_hostnames: list[str] = Field(min_length=1)
    execution_mode: Literal["config", "exec"]
    commands: list[str] = Field(min_length=1)
    verification_commands: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_targets(self):
        if "*" in self.device_hostnames and self.device_hostnames != ["*"]:
            raise ValueError("'*' cannot be mixed with explicit device hostnames")
        return self


class AIAction(BaseModel):
    """The only plan shape the model is allowed to return.

    The AI never executes anything: this object is a *proposal* that the policy
    engine and the change workflow then accept or refuse.
    """

    model_config = ConfigDict(extra="ignore")

    intent: Literal["monitor", "configure", "troubleshoot"]
    operations: list[AIOperation] = Field(min_length=1)
    explanation: str


def build_ai_action_schema(device_hostnames: Iterable[str]) -> dict:
    """Build the provider schema from the inventory visible to the model."""
    target_branches = [
        {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {"type": "string", "enum": ["*"]},
        }
    ]
    known_hostnames = sorted(set(device_hostnames))
    if known_hostnames:
        target_branches.append(
            {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": known_hostnames},
            }
        )

    # google-genai's response_json_schema accepts standard JSON Schema names
    # and only a documented subset of keywords. Inventory enums encode the
    # explicit-target branch without relying on unsupported pattern/not rules.
    return {
        "type": "object",
        "required": ["intent", "operations", "explanation"],
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["monitor", "configure", "troubleshoot"],
            },
            "operations": {
                "type": "array",
                # Provider decoding must be able to emit the deliberate refusal
                # handled by AIService.interpret(). AIAction itself intentionally
                # remains stricter (min_length=1) after that branch is handled.
                "minItems": 0,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "device_hostnames",
                        "execution_mode",
                        "commands",
                    ],
                    "properties": {
                        "device_hostnames": {"anyOf": target_branches},
                        "execution_mode": {
                            "type": "string",
                            "enum": ["config", "exec"],
                        },
                        "commands": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                        "verification_commands": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "explanation": {"type": "string"},
        },
    }


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    session_id: int | None = None
