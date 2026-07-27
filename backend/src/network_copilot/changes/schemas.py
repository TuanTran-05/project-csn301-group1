from pydantic import BaseModel, ConfigDict, Field


class ChangePreviewSchema(BaseModel):
    """Request body for POST /api/changes/preview."""

    model_config = ConfigDict(extra="forbid")

    device_id: int
    commands: list[str] = Field(min_length=1)
    verification_commands: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=255)
