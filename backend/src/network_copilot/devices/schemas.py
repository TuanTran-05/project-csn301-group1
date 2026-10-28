import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

HOSTNAME_PATTERN = re.compile(r"^[A-Z0-9-]+$")

DeviceType = Literal["cisco_ios", "cisco_asa", "paloalto_panos", "generic"]
DeviceRole = Literal[
    "isp", "firewall", "core", "distribution", "access", "dmz", "management",
    "host", "server", "internet", "wireless", "segment",
]
DeviceKind = Literal[
    "router", "l3_switch", "switch", "firewall", "pc", "server", "cloud", "ap", "segment"
]
Lifecycle = Literal["planned", "provisioned"]

_KIND_BY_ROLE = {"core": "router", "distribution": "l3_switch", "firewall": "firewall"}
_UNMANAGED_DEFAULTS = {
    "pc": "host", "server": "server", "cloud": "internet", "ap": "wireless", "segment": "segment",
}
DeviceEnvironment = Literal["pnetlab", "physical", "virtual", "physical_lab", "production"]


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


def _validate_data_ip(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(ipaddress.ip_interface(value))
    except ValueError as exc:
        raise ValueError("data_ip must be an address with prefix, e.g. 192.168.10.50/24") from exc


def _validate_gateway(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError as exc:
        raise ValueError("data_gateway must be a valid IPv4 address") from exc


class DeviceCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    hostname: str = Field(min_length=1, max_length=64)
    # Optional for unmanaged devices and for devices that are only planned.
    management_ip: str | None = None
    device_type: DeviceType
    role: DeviceRole
    kind: DeviceKind | None = None
    lifecycle: Lifecycle = "provisioned"
    data_ip: str | None = None
    data_gateway: str | None = None
    ssh_port: int = Field(default=22, ge=1, le=65535)
    monitoring_enabled: bool = True
    environment: DeviceEnvironment = "pnetlab"
    isolated: bool = False
    description: str | None = Field(default=None, max_length=255)
    credential: CredentialSchema | None = None
    pos_x: float | None = Field(default=None, ge=-100000, le=100000)
    pos_y: float | None = Field(default=None, ge=-100000, le=100000)

    _check_hostname = field_validator("hostname")(_validate_hostname)
    _check_data_ip = field_validator("data_ip")(_validate_data_ip)
    _check_gateway = field_validator("data_gateway")(_validate_gateway)

    @field_validator("management_ip")
    @classmethod
    def check_management_ip(cls, value: str | None, info: ValidationInfo) -> str | None:
        return None if value in (None, "") else _validate_management_ip(value, info)

    @model_validator(mode="after")
    def check_kind_and_lifecycle(self):
        unmanaged = self.device_type == "generic"
        if unmanaged:
            if self.management_ip:
                raise ValueError("an unmanaged device has no management_ip")
            if self.credential is not None:
                raise ValueError("an unmanaged device has no SSH credential")
            if self.kind is not None and self.kind not in _UNMANAGED_DEFAULTS:
                raise ValueError(f"kind {self.kind} needs a Cisco device_type")
            self.kind = self.kind or {"host": "pc", "server": "server", "internet": "cloud",
                                      "wireless": "ap", "segment": "segment"}.get(self.role, "pc")
            self.lifecycle = "provisioned"
        else:
            if self.kind is not None and self.kind in _UNMANAGED_DEFAULTS:
                raise ValueError(f"kind {self.kind} is unmanaged; use device_type generic")
            self.kind = self.kind or _KIND_BY_ROLE.get(self.role, "switch")
            if self.lifecycle == "provisioned" and not self.management_ip:
                raise ValueError("management_ip is required unless the device is planned")
        return self


class DeviceUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    hostname: str | None = Field(default=None, min_length=1, max_length=64)
    management_ip: str | None = None
    device_type: DeviceType | None = None
    role: DeviceRole | None = None
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    monitoring_enabled: bool | None = None
    environment: DeviceEnvironment | None = None
    isolated: bool | None = None
    description: str | None = Field(default=None, max_length=255)
    credential: CredentialSchema | None = None
    kind: DeviceKind | None = None
    data_ip: str | None = None
    data_gateway: str | None = None
    pos_x: float | None = Field(default=None, ge=-100000, le=100000)
    pos_y: float | None = Field(default=None, ge=-100000, le=100000)

    _check_data_ip = field_validator("data_ip")(_validate_data_ip)
    _check_gateway = field_validator("data_gateway")(_validate_gateway)

    @field_validator("hostname")
    @classmethod
    def check_hostname(cls, value: str | None) -> str | None:
        return None if value is None else _validate_hostname(value)

    @field_validator("management_ip")
    @classmethod
    def check_ip(cls, value: str | None, info: ValidationInfo) -> str | None:
        return None if value is None else _validate_management_ip(value, info)
