"""Session CRUD and listing with account ownership enforced on API requests."""

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
    from ..governance.visibility import request_user
    from ..governance.access import role
    from ..errors import ForbiddenError
    user = request_user()
    if session_id is not None:
        session = db.session.get(ChatSession, session_id)
        if session is not None:
            if user and role(user) != "ADMIN" and session.created_by_id != user.id:
                raise ForbiddenError("This chat belongs to another account.")
            return session

    query = db.session.query(ChatSession)
    if user and role(user) != "ADMIN":
        query = query.filter_by(created_by_id=user.id)
    latest = (
        query
        .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
        .first()
    )
    if latest is not None:
        return latest
    return create_session(created_by_id=user.id if user else None)


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
    from ..governance.visibility import request_user
    from ..governance.access import role
    user = request_user()
    query = db.session.query(ChatSession)
    if user and role(user) != "ADMIN":
        query = query.filter_by(created_by_id=user.id)
    sessions = query.all()
    sessions.sort(key=_last_activity, reverse=True)
    return [session_to_dict(session) for session in sessions]
