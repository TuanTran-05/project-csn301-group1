import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Same shape the change-capability parser accepts for "interface <name>".
INTERFACE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z-]*\d[\d/.:]*$")

LinkType = Literal["physical", "routed", "trunk"]


def normalize_interface(value: str) -> str:
    value = "".join(value.split())
    if not INTERFACE_PATTERN.match(value):
        raise ValueError(
            "interface must look like GigabitEthernet0/1 (letters, then digits, "
            "'/', '.' or ':')"
        )
    return value


class LinkSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_a_id: int
    # Left out, an interface is chosen for you: the lowest free port.
    interface_a: str | None = Field(default=None, min_length=1, max_length=40)
    device_b_id: int
    interface_b: str | None = Field(default=None, min_length=1, max_length=40)
    link_type: LinkType = "physical"
    network: str | None = None
    ip_a: str | None = None
    ip_b: str | None = None
    allowed_vlans: str | None = Field(default=None, max_length=255)
    bring_up: bool = True
    description: str | None = Field(default=None, max_length=255)

    @field_validator("interface_a", "interface_b")
    @classmethod
    def check_interface(cls, value: str | None) -> str | None:
        return None if value is None else normalize_interface(value)


class LinkUpdateSchema(BaseModel):
    """Partial update. Endpoints are fixed: delete and recreate to re-cable."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    link_type: LinkType | None = None
    network: str | None = None
    ip_a: str | None = None
    ip_b: str | None = None
    allowed_vlans: str | None = Field(default=None, max_length=255)
    bring_up: bool | None = None
    description: str | None = Field(default=None, max_length=255)


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int
    x: float = Field(ge=-100000, le=100000)
    y: float = Field(ge=-100000, le=100000)


class LayoutSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    positions: list[Position] = Field(max_length=500)


class QuickDeviceSchema(BaseModel):
    """Drop a templated device on the canvas."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    template: str = Field(min_length=1, max_length=32)
    x: float | None = Field(default=None, ge=-100000, le=100000)
    y: float | None = Field(default=None, ge=-100000, le=100000)
    environment: Literal["pnetlab", "physical"] | None = None
    credential: dict | None = None


class ConfigRequestSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    link_ids: list[int] | None = Field(default=None, max_length=1000)
