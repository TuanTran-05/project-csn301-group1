"""A cheap fingerprint of everything the chat page shows for one project.

The page used to ask for devices, changes, batches and messages every few
seconds. With these fingerprints it asks "did anything change?" (or is told, over
server-sent events) and refetches only the part that did.
"""

import hashlib

from ..changes.model import ChangeBatch, ChangeRequest
from ..chat.model import ChatMessage, ChatSession
from ..devices.model import Device
from ..extensions import db
from ..topology.model import TopologyLink, TopologyNote, TopologyVersion

RECENT = 500  # rows per list; older ones cannot change the page


def _digest(value) -> str:
    return hashlib.sha1(repr(value).encode()).hexdigest()[:12]


def project_state(project_id: int) -> dict[str, str]:
    messages = (
        db.session.query(db.func.max(ChatMessage.id), db.func.count(ChatMessage.id))
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .filter(ChatSession.project_id == project_id)
        .one()
    )
    changes = (
        db.session.query(ChangeRequest.id, ChangeRequest.status)
        .join(Device, Device.id == ChangeRequest.device_id)
        .filter(Device.project_id == project_id)
        .order_by(ChangeRequest.id.desc())
        .limit(RECENT)
        .all()
    )
    batches = (
        db.session.query(ChangeBatch.id, ChangeBatch.status)
        .filter(ChangeBatch.project_id == project_id)
        .order_by(ChangeBatch.id.desc())
        .limit(RECENT)
        .all()
    )
    devices = (
        db.session.query(Device.id, Device.status, Device.updated_at, Device.hostname)
        .filter(Device.project_id == project_id)
        .order_by(Device.id)
        .all()
    )
    sessions = (
        db.session.query(db.func.count(ChatSession.id))
        .filter(ChatSession.project_id == project_id)
        .scalar()
    )
    layout = (
        db.session.query(
            Device.id, Device.pos_x, Device.pos_y, Device.kind, Device.lifecycle, Device.role,
            Device.data_ip, Device.data_gateway,
        )
        .filter(Device.project_id == project_id)
        .order_by(Device.id)
        .all()
    )
    links = (
        db.session.query(
            TopologyLink.id, TopologyLink.link_type, TopologyLink.interface_a, TopologyLink.interface_b,
            TopologyLink.network, TopologyLink.ip_a, TopologyLink.ip_b, TopologyLink.allowed_vlans,
            TopologyLink.nameif_a, TopologyLink.nameif_b,
        )
        .filter(TopologyLink.project_id == project_id)
        .order_by(TopologyLink.id)
        .all()
    )
    notes = (
        db.session.query(TopologyNote.id, TopologyNote.text, TopologyNote.x, TopologyNote.y,
                         TopologyNote.width, TopologyNote.height, TopologyNote.color)
        .filter(TopologyNote.project_id == project_id)
        .order_by(TopologyNote.id)
        .all()
    )
    versions = (
        db.session.query(db.func.max(TopologyVersion.id), db.func.count(TopologyVersion.id))
        .filter(TopologyVersion.project_id == project_id)
        .one()
    )
    return {
        "topology": _digest(([tuple(r) for r in layout], [tuple(r) for r in links],
                             [tuple(r) for r in notes], tuple(versions))),
        "messages": _digest((tuple(messages), sessions)),
        "changes": _digest([tuple(row) for row in changes]),
        "batches": _digest([tuple(row) for row in batches]),
        "devices": _digest([tuple(row) for row in devices]),
    }


def combined(state: dict[str, str]) -> str:
    return _digest(sorted(state.items()))
