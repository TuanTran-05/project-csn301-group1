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
    return {
        "messages": _digest((tuple(messages), sessions)),
        "changes": _digest([tuple(row) for row in changes]),
        "batches": _digest([tuple(row) for row in batches]),
        "devices": _digest([tuple(row) for row in devices]),
    }


def combined(state: dict[str, str]) -> str:
    return _digest(sorted(state.items()))
