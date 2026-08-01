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


# Sent to providers that can constrain decoding server-side. Asking only for
# "some JSON" was not enough: gemini-3.5-flash still produced a repeated
# fragment mid-string. A schema makes the shape a guarantee, not a request.
AI_ACTION_SCHEMA = {
    "type": "OBJECT",
    "required": ["intent", "operations", "explanation"],
    "properties": {
        "intent": {
            "type": "STRING",
            "enum": ["monitor", "configure", "troubleshoot"],
        },
        "operations": {
            "type": "ARRAY",
            # Provider decoding must be able to emit the deliberate refusal
            # handled by AIService.interpret(). AIAction itself intentionally
            # remains stricter (min_length=1) after that branch is handled.
            "minItems": 0,
            "items": {
                "type": "OBJECT",
                "additionalProperties": False,
                "required": ["device_hostnames", "execution_mode", "commands"],
                "properties": {
                    "device_hostnames": {
                        # These are keywords preserved by google-genai's
                        # response_json_schema path. The second branch accepts
                        # every string except the single wildcard token, so
                        # "*" can only appear alone via the first branch.
                        "anyOf": [
                            {
                                "type": "ARRAY",
                                "minItems": 1,
                                "maxItems": 1,
                                "items": {"type": "STRING", "enum": ["*"]},
                            },
                            {
                                "type": "ARRAY",
                                "minItems": 1,
                                "items": {
                                    "type": "STRING",
                                    "pattern": r"^($|[^*]|.{2,})$",
                                },
                            },
                        ],
                    },
                    "execution_mode": {
                        "type": "STRING",
                        "enum": ["config", "exec"],
                    },
                    "commands": {
                        "type": "ARRAY",
                        "minItems": 1,
                        "items": {"type": "STRING"},
                    },
                    "verification_commands": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                    },
                },
            },
        },
        "explanation": {"type": "STRING"},
    },
}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
