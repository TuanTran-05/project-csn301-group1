"""Keep NetAI's own way into a device.

NetAI reaches every device over SSH on its management address. A change that
re-addresses, shuts or re-purposes the interface holding that address cuts
NetAI off, and the device then has to be repaired by hand on its console. For
a PC emulated by an IOSv-L2 switch the risk is real: the bring-up puts the
management address on interface Vlan1, and "give PC1 an address" is most
naturally read as configuring Vlan1. Such a change is refused before anyone
approves it, with a message that says which port to name instead.
"""

from __future__ import annotations

import re

from ..errors import PolicyViolationError
from ..extensions import db
from ..topology.interfaces import normalize_label


class ManagementInterfaceError(PolicyViolationError):
    """A change would touch the interface NetAI uses to reach the device."""

    error = "management_interface"


_INTERFACE_LINE = re.compile(r"^interface\s+(?P<name>.+?)\s*$", re.I)
_REMOVE_INTERFACE = re.compile(r"^(?:no|default)\s+interface\s+(?P<name>.+?)\s*$", re.I)
_SAFE_INSIDE = re.compile(r"^(?:description\b|no\s+description\b)", re.I)
_LEAVES_INTERFACE = re.compile(r"^(?:exit|end|!)$", re.I)


def canonical(name: str | None) -> str:
    """One spelling per port: "Gi0/1" and "GigabitEthernet0/1" agree, "vlan 1" is "vlan1"."""
    text = "".join((name or "").split()).lower()
    vlan = re.fullmatch(r"(?:vlan|vl)(\d+)", text)
    if vlan:
        return f"vlan{int(vlan.group(1))}"
    full = normalize_label(text)
    return full.lower() if full else text


def _from_backup(device) -> str | None:
    """The interface whose address is the management IP, in the newest backup."""
    from ..backups.model import ConfigBackup

    backup = (
        db.session.query(ConfigBackup)
        .filter_by(device_id=device.id)
        .order_by(ConfigBackup.id.desc())
        .first()
    )
    if backup is None or not backup.running_config:
        return None
    current = None
    address = re.compile(rf"^\s+ip\s+address\s+{re.escape(device.management_ip)}\s", re.I)
    for line in backup.running_config.splitlines():
        match = _INTERFACE_LINE.match(line)
        if match:
            current = match.group("name")
            continue
        if line and not line[0].isspace():
            current = None
        elif current and address.match(line):
            return current
    return None


def management_interface(device) -> str | None:
    """The interface holding the device's management address, when it is known.

    The newest backup says it outright. Without one, a PNETLab switch NetAI
    brought up keeps it on Vlan1 (topology/bootstrap.py).
    """
    if not device.management_ip:
        return None
    found = _from_backup(device)
    if found:
        return found
    if device.environment == "pnetlab" and device.kind == "switch":
        return "Vlan1"
    return None


def check(device, commands: list[str]) -> None:
    """Refuse configuration that changes the management interface.

    Entering the interface is fine and so is a description; any other command
    under it, or removing or defaulting the interface, is refused.
    """
    interface = management_interface(device)
    if not interface:
        return
    target = canonical(interface)
    inside = False
    for raw in commands:
        command = " ".join(str(raw).split())
        removed = _REMOVE_INTERFACE.match(command)
        if removed and canonical(removed.group("name")) == target:
            _refuse(device, interface, command)
        entered = _INTERFACE_LINE.match(command)
        if entered:
            names = [part for part in re.split(r"\s*,\s*", entered.group("name"))]
            inside = any(canonical(name) == target for name in names)
            continue
        if _LEAVES_INTERFACE.match(command):
            inside = False
            continue
        if inside and not _SAFE_INSIDE.match(command):
            _refuse(device, interface, command)


def _language() -> str:
    """The page's VI / EN choice, when the change comes from the chat."""
    from flask import g, has_app_context

    return (g.get("ui_language") if has_app_context() else None) or "vi"


def _refuse(device, interface: str, command: str) -> None:
    if _language() == "en":
        text = (
            f"No change created for {device.hostname}: the command \"{command}\" touches {interface}, "
            f"the port holding the management address NetAI uses to reach the device. NetAI would "
            f"lose its connection and the device would have to be fixed on the console. Name the "
            f"port to configure, for example \"on port Gi0/0 to the switch\"."
        )
    else:
        text = (
            f"Không tạo thay đổi cho {device.hostname}: lệnh \"{command}\" đụng vào {interface}, "
            f"cổng đang giữ IP quản lý mà NetAI dùng để kết nối tới thiết bị. Làm vậy NetAI sẽ mất "
            f"kết nối và phải sửa tay trên console. Hãy ghi rõ cổng cần cấu hình, ví dụ "
            f"\"trên cổng Gi0/0 nối với switch\"."
        )
    raise ManagementInterfaceError(
        text, {"device": device.hostname, "interface": interface, "command": command},
    )
