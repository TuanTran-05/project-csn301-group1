"""Shared team chat transcript with the AI copilot.

Every exchange through POST /api/ai/chat is recorded here, whether it
succeeded, was blocked by the policy engine, or failed upstream, so anyone
reopening the page sees exactly what happened, not just the calls that
returned 200.
"""

import logging

from ..extensions import db
from .model import ChatMessage

logger = logging.getLogger(__name__)


def record_message(
    user_id: int | None,
    username: str | None,
    role: str,
    content: str,
    payload: dict | None = None,
) -> ChatMessage | None:
    """Persist one chat message. Never raises: a failure to record history
    must not break the AI response the user is waiting for."""
    try:
        message = ChatMessage(
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


def list_messages(limit: int = 200) -> list[ChatMessage]:
    return (
        db.session.query(ChatMessage)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
