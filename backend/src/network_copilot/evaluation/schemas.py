import json
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

class CorpusCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    language: str
    category: str
    message: str = Field(min_length=3, max_length=2000)
    actor_role: str = "ADMIN"
    expected_intent: str
    expected_targets: list[str] = []
    expected_execution_mode: str | None = None
    expected_command_patterns: list[str] = []
    expected_capability_tier: str | None = None
    expected_backend_outcome: str
    must_require_approval: bool
    must_require_confirmation: bool = False
    must_not_open_ssh_during_ai_request: bool = True
    @model_validator(mode="after")
    def shape(self):
        if self.expected_intent == "chat" and self.expected_targets: raise ValueError("chat cases cannot declare targets")
        if self.expected_intent != "chat" and self.expected_backend_outcome == "accepted" and not self.expected_targets: raise ValueError("accepted actions require targets")
        return self

def load_corpus(path: Path) -> list[CorpusCase]:
    return [CorpusCase.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]
