from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AIAction(BaseModel):
    """The only shape the model is allowed to return.

    The AI never executes anything: this object is a *proposal* that the policy
    engine and the change workflow then accept or refuse.
    """

    model_config = ConfigDict(extra="ignore")

    intent: Literal["monitor", "configure", "troubleshoot"]
    device_hostname: str
    commands: list[str] = Field(min_length=1)
    verification_commands: list[str] = []
    explanation: str


# Sent to providers that can constrain decoding server-side. Asking only for
# "some JSON" was not enough: gemini-3.5-flash still produced a repeated
# fragment mid-string. A schema makes the shape a guarantee, not a request.
AI_ACTION_SCHEMA = {
    "type": "OBJECT",
    "required": ["intent", "device_hostname", "commands", "explanation"],
    "properties": {
        "intent": {
            "type": "STRING",
            "enum": ["monitor", "configure", "troubleshoot"],
        },
        "device_hostname": {"type": "STRING"},
        "commands": {"type": "ARRAY", "items": {"type": "STRING"}},
        "verification_commands": {"type": "ARRAY", "items": {"type": "STRING"}},
        "explanation": {"type": "STRING"},
    },
}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
