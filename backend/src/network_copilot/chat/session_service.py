"""Session CRUD and listing shared with everyone in a project."""

from ..extensions import db
from .model import ChatMessage, ChatSession

_TITLE_MAX_LENGTH = 60


def create_session(project_id: int, created_by_id: int | None = None) -> ChatSession:
    session = ChatSession(project_id=project_id, created_by_id=created_by_id)
    db.session.add(session)
    db.session.commit()
    return session


def get_session(session_id: int, project_id: int) -> ChatSession | None:
    """The session, only if it belongs to ``project_id``."""
    session = db.session.get(ChatSession, session_id)
    if session is None or session.project_id != project_id:
        return None
    return session


def resolve_or_create_session(
    session_id: int | None, project_id: int
) -> ChatSession:
    """Return the session for session_id, or a sensible default.

    Used wherever a session_id is optional at the API layer (see the
    "Deviation from the spec" note in the plan's Global Constraints): an
    unknown or omitted id - or one that belongs to another project - falls
    back to the project's most recently created session, creating a brand new
    one only if the project has none at all.
    """
    if session_id is not None:
        session = get_session(session_id, project_id)
        if session is not None:
            return session

    latest = (
        db.session.query(ChatSession)
        .filter(ChatSession.project_id == project_id)
        .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
        .first()
    )
    if latest is not None:
        return latest
    return create_session(project_id)


def _title_for_session(session_id: int) -> str:
    first = (
        db.session.query(ChatMessage.content)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .first()
    )
    return _title_from_content(first[0] if first is not None else None)


def _title_from_content(content: str | None) -> str:
    if not content:
        return "New chat"
    content = content.strip()
    if not content:
        return "New chat"
    if len(content) <= _TITLE_MAX_LENGTH:
        return content
    return content[:_TITLE_MAX_LENGTH].rstrip() + "…"


def session_to_dict(session: ChatSession) -> dict:
    return {
        "id": session.id,
        "title": _title_for_session(session.id),
        "created_at": session.created_at.isoformat() if session.created_at else None,
    }


def list_sessions(project_id: int) -> list[dict]:
    activity = (
        db.session.query(
            ChatMessage.session_id.label("session_id"),
            db.func.max(ChatMessage.created_at).label("last_activity"),
        )
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .filter(ChatSession.project_id == project_id)
        .group_by(ChatMessage.session_id)
        .subquery()
    )
    first_content = (
        db.session.query(ChatMessage.content)
        .filter(ChatMessage.session_id == ChatSession.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(1)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    last_activity = db.func.coalesce(activity.c.last_activity, ChatSession.created_at)
    rows = (
        db.session.query(ChatSession, first_content.label("first_content"))
        .outerjoin(activity, activity.c.session_id == ChatSession.id)
        .filter(ChatSession.project_id == project_id)
        .order_by(last_activity.desc(), ChatSession.id.asc())
        .all()
    )
    return [
        {
            "id": session.id,
            "title": _title_from_content(content),
            "created_at": session.created_at.isoformat() if session.created_at else None,
        }
        for session, content in rows
    ]
