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


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
