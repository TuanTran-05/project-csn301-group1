"""Ask which port to use when a request to address devices names none.

"Đặt IP cho PC1 172.20.5.163/28" says what but not where. Guessing is risky:
a PC emulated by an IOSv-L2 switch keeps NetAI's management address on Vlan1,
the most natural place to put a switch's address. So before the model sees
such a message, NetAI answers with the ports each named device is cabled on
(from the project diagram), the likely one already picked; the chat page lets
the user confirm and sends the request again with the ports spelled out.

Only devices addressed on a physical port are asked about: a router, a
firewall, or a switch with a single cable (a PC stand-in). A switch with
several cables is normally given an SVI, which the model handles, and the
management-interface guard (changes/management_guard.py) stops it touching
the one NetAI uses.
"""

from __future__ import annotations

import re

from ..devices.model import Device
from ..extensions import db
from ..topology.model import TopologyLink

_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
_SET_ADDRESS = re.compile(
    r"\b(?:đặt|gán|cấu\s*hình|dat|gan|cau\s*hinh|set|assign|configure|config)\s+"
    r"(?:lại\s+|lai\s+)?(?:địa\s*chỉ\s+|dia\s*chi\s+)?ip\b"
    r"|\bip\s+address\b",
    re.I,
)
# Any sign the user already said where: a port name, an SVI, a subinterface.
_SAYS_WHERE = re.compile(
    r"\b(?:gigabitethernet|fastethernet|tengigabitethernet|ethernet|gig|gi|fa|te|eth|e|g)"
    r"\s?\d+(?:/\d+)+(?:\.\d+)?\b"
    r"|\b(?:interface|cổng|cong|port|svi|trên|tren|on)\s+vlan\s?\d+\b"
    r"|\binterface\s+\S+"
    r"|\bsub-?interface|\bloopback",
    re.I,
)
_ASKS_FOR = ("router", "firewall")
_NOT_A_PEER = ("cloud",)


def _short(name: str) -> str:
    return (name or "").replace("GigabitEthernet", "Gi").replace("FastEthernet", "Fa")


def _named(message: str, devices: list[Device]) -> list[Device]:
    """Project devices whose hostname appears in the message as a whole word."""
    found = []
    for device in devices:
        pattern = rf"(?<![\w-]){re.escape(device.hostname)}(?![\w-])"
        if re.search(pattern, message, re.I):
            found.append(device)
    return found


_QUESTION = {
    "vi": ("Bạn muốn đặt IP trên cổng nào? Mình đã chọn sẵn cổng đang nối dây trên sơ đồ cho "
           "từng thiết bị. Kiểm tra lại rồi bấm Tiếp tục."),
    "en": ("Which port should the address go on? The cabled port on the diagram is already "
           "picked for each device. Check it, then click Continue."),
}


def ask(message: str, project, language: str = "vi") -> dict | None:
    """The question to ask, or None when the request is clear enough as it is."""
    if not _SET_ADDRESS.search(message) or not _IP.search(message) or _SAYS_WHERE.search(message):
        return None
    devices = (
        db.session.query(Device).filter(Device.project_id == project.id)
        .order_by(Device.hostname).all()
    )
    named = _named(message, devices)
    if not named:
        return None
    by_id = {device.id: device for device in devices}
    links = db.session.query(TopologyLink).filter(TopologyLink.project_id == project.id).all()

    choices = []
    for device in named:
        if not device.managed:
            continue
        cables = []
        for link in links:
            if link.device_a_id == device.id:
                mine, peer, peer_if = link.interface_a, by_id.get(link.device_b_id), link.interface_b
            elif link.device_b_id == device.id:
                mine, peer, peer_if = link.interface_b, by_id.get(link.device_a_id), link.interface_a
            else:
                continue
            if peer is None or peer.kind in _NOT_A_PEER:
                continue
            cables.append({
                "interface": mine, "short": _short(mine),
                "peer": peer.hostname, "peer_interface": _short(peer_if),
            })
        if not cables:
            continue
        if device.kind not in _ASKS_FOR and not (device.kind == "switch" and len(cables) == 1):
            continue
        cables.sort(key=lambda item: item["interface"])
        choices.append({"hostname": device.hostname, "options": cables, "suggested": cables[0]["interface"]})

    if not choices:
        return None
    return {
        "intent": "chat",
        "explanation": _QUESTION.get(language, _QUESTION["vi"]),
        "requires_approval": False,
        "diagram": None,
        "port_choice": {"message": message, "devices": choices},
    }
