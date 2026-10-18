import ipaddress
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Environment = Literal["pnetlab", "physical", "mixed"]
MemberAccess = Literal["viewer", "editor"]

# A management network is where the backend opens SSH sessions, so a wildly
# wide or non-private range is refused: it would let a project owner aim the
# backend at arbitrary hosts.
MIN_PREFIX_LENGTH = 16


def validate_management_network(value: str) -> str:
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValueError("management_network must be a valid IPv4 CIDR") from exc
    if network.version != 4:
        raise ValueError("management_network must be an IPv4 network")
    if (
        network.is_loopback
        or network.is_link_local
        or network.is_multicast
        or network.is_unspecified
        or not network.is_private
    ):
        raise ValueError(
            "management_network must be a private (RFC 1918) range that is not "
            "loopback or link-local"
        )
    if network.prefixlen < MIN_PREFIX_LENGTH:
        raise ValueError(
            f"management_network must be /{MIN_PREFIX_LENGTH} or smaller"
        )
    return str(network)


def validate_addressing_pool(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValueError("addressing_pool must be a valid IPv4 CIDR") from exc
    if network.version != 4 or not network.is_private or network.prefixlen < 8 or network.prefixlen > 24:
        raise ValueError("addressing_pool must be a private IPv4 range between /8 and /24")
    return str(network)


class ProjectCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=255)
    management_network: str
    environment: Environment = "pnetlab"
    # Range that automatic addressing (link /30s, LAN subnets) draws from.
    addressing_pool: str | None = None

    _check_network = field_validator("management_network")(validate_management_network)
    _check_pool = field_validator("addressing_pool")(validate_addressing_pool)


class ProjectUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=255)
    management_network: str | None = None
    environment: Environment | None = None
    addressing_pool: str | None = None
    ai_management_enabled: bool = False

    _check_pool = field_validator("addressing_pool")(validate_addressing_pool)

    @field_validator("management_network")
    @classmethod
    def check_network(cls, value: str | None) -> str | None:
        return None if value is None else validate_management_network(value)


class MemberSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=64)
    access: MemberAccess = "viewer"


class MemberUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access: MemberAccess


