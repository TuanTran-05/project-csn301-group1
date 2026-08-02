"""Session CRUD and listing for the shared team chat."""

from ..extensions import db
from .model import ChatMessage, ChatSession

_TITLE_MAX_LENGTH = 60


def create_session(created_by_id: int | None = None) -> ChatSession:
    session = ChatSession(created_by_id=created_by_id)
    db.session.add(session)
    db.session.commit()
    return session


def resolve_or_create_session(session_id: int | None) -> ChatSession:
    """Return the session for session_id, or a sensible default.

    Used wherever a session_id is optional at the API layer (see the
    "Deviation from the spec" note in the plan's Global Constraints): an
    unknown or omitted id falls back to the most recently created session,
    creating a brand new one only if none exist at all.
    """
    if session_id is not None:
        session = db.session.get(ChatSession, session_id)
        if session is not None:
            return session

    latest = (
        db.session.query(ChatSession)
        .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
        .first()
    )
    if latest is not None:
        return latest
    return create_session()


def _title_for_session(session_id: int) -> str:
    first = (
        db.session.query(ChatMessage.content)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .first()
    )
    if first is None or not first[0]:
        return "New chat"
    content = first[0].strip()
    if len(content) <= _TITLE_MAX_LENGTH:
        return content
    return content[:_TITLE_MAX_LENGTH].rstrip() + "…"


def session_to_dict(session: ChatSession) -> dict:
    return {
        "id": session.id,
        "title": _title_for_session(session.id),
        "created_at": session.created_at.isoformat() if session.created_at else None,
    }


def _last_activity(session: ChatSession):
    last = (
        db.session.query(db.func.max(ChatMessage.created_at))
        .filter(ChatMessage.session_id == session.id)
        .scalar()
    )
    return last or session.created_at


def list_sessions() -> list[dict]:
    sessions = db.session.query(ChatSession).all()
    sessions.sort(key=_last_activity, reverse=True)
    return [session_to_dict(session) for session in sessions]
