"""Shared team chat transcript with the AI copilot.

Every exchange through POST /api/ai/chat is recorded here, whether it
succeeded, was blocked by the policy engine, or failed upstream, so anyone
reopening the page sees exactly what happened, not just the calls that
returned 200. Every message belongs to exactly one chat session (see
chat/session_service.py); a caller that does not know/care which session
gets a sensible default rather than being forced to resolve one itself.
"""

import logging

from ..extensions import db
from . import session_service
from .model import ChatMessage

logger = logging.getLogger(__name__)


def record_message(
    user_id: int | None,
    username: str | None,
    role: str,
    content: str,
    payload: dict | None = None,
    session_id: int | None = None,
) -> ChatMessage | None:
    """Persist one chat message. Never raises: a failure to record history
    must not break the AI response the user is waiting for."""
    try:
        session = session_service.resolve_or_create_session(session_id)
        message = ChatMessage(
            session_id=session.id,
            user_id=user_id,
            username=username,
            role=role,
            content=content or "",
            payload=payload,
        )
        db.session.add(message)
        db.session.commit()
        return message
    except Exception:  # pragma: no cover - defensive, matches audit.service
        logger.exception("Failed to record chat message (role=%s)", role)
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def list_messages(session_id: int | None = None, limit: int = 200) -> list[ChatMessage]:
    session = session_service.resolve_or_create_session(session_id)
    bounded_ids = (
        db.session.query(ChatMessage.id.label("id"))
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(min(max(limit, 1), 500))
        .subquery()
    )
    return (
        db.session.query(ChatMessage)
        .join(bounded_ids, ChatMessage.id == bounded_ids.c.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )
