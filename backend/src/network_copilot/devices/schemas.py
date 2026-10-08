import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

HOSTNAME_PATTERN = re.compile(r"^[A-Z0-9-]+$")

DeviceType = Literal["cisco_ios", "cisco_asa"]
DeviceRole = Literal[
    "isp", "firewall", "core", "distribution", "access", "dmz", "management"
]
DeviceEnvironment = Literal["pnetlab", "physical"]


def management_network(info: ValidationInfo | None = None) -> ipaddress.IPv4Network:
    """The network a management IP must be in.

    Callers validating a device for a project pass that project's network as
    ``context={"network": ...}``. Without it the app-wide default applies.
    """
    context = getattr(info, "context", None) or {}
    declared = context.get("network")
    if declared is not None:
        return ipaddress.ip_network(declared, strict=False)

    from flask import current_app, has_app_context

    default = "172.16.3.0/24"
    if has_app_context():
        default = current_app.config.get("MANAGEMENT_NETWORK", default)
    return ipaddress.ip_network(default, strict=False)


def _validate_hostname(value: str) -> str:
    if not HOSTNAME_PATTERN.match(value):
        raise ValueError(
            "hostname may only contain uppercase letters, digits and hyphens"
        )
    return value


def _validate_management_ip(value: str, info: ValidationInfo) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("management_ip must be a valid IPv4 address") from exc

    network = management_network(info)
    if address not in network:
        raise ValueError(f"management_ip must be inside {network}")
    return str(address)


class CredentialSchema(BaseModel):
    """SSH login stored (encrypted) together with the device."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    enable_secret: str | None = Field(default=None, max_length=256)


class DeviceCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    hostname: str = Field(min_length=1, max_length=64)
    management_ip: str
    device_type: DeviceType
    role: DeviceRole
    ssh_port: int = Field(default=22, ge=1, le=65535)
    monitoring_enabled: bool = True
    environment: DeviceEnvironment = "pnetlab"
    description: str | None = Field(default=None, max_length=255)
    credential: CredentialSchema | None = None
    pos_x: float | None = Field(default=None, ge=-100000, le=100000)
    pos_y: float | None = Field(default=None, ge=-100000, le=100000)

    _check_hostname = field_validator("hostname")(_validate_hostname)
    _check_ip = field_validator("management_ip")(_validate_management_ip)


class DeviceUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    hostname: str | None = Field(default=None, min_length=1, max_length=64)
    management_ip: str | None = None
    device_type: DeviceType | None = None
    role: DeviceRole | None = None
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    monitoring_enabled: bool | None = None
    environment: DeviceEnvironment | None = None
    description: str | None = Field(default=None, max_length=255)
    credential: CredentialSchema | None = None

    @field_validator("hostname")
    @classmethod
    def check_hostname(cls, value: str | None) -> str | None:
        return None if value is None else _validate_hostname(value)

    @field_validator("management_ip")
    @classmethod
    def check_ip(cls, value: str | None, info: ValidationInfo) -> str | None:
        return None if value is None else _validate_management_ip(value, info)
