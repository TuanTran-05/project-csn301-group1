"""Session CRUD and listing, private to the user who started each session.

A project's members share its devices and topology, but every member keeps
their own conversations with the assistant: a Viewer never reads or writes
into an Operator's session, and vice versa. ADMIN may open any session of a
project by id for auditing, but still lists only their own.
"""

from ..extensions import db
from .model import ChatMessage, ChatSession

_TITLE_MAX_LENGTH = 60


def create_session(project_id: int, created_by_id: int | None = None) -> ChatSession:
    session = ChatSession(project_id=project_id, created_by_id=created_by_id)
    db.session.add(session)
    db.session.commit()
    return session


def can_use_session(user, session: ChatSession, write: bool = False) -> bool:
    """Whether ``user`` may read (or, with ``write``, post to) ``session``.

    ``user=None`` is an internal caller. Only the session's creator may post
    to it; ADMIN may additionally read any session for auditing.
    """
    if user is None or session.created_by_id == user.id:
        return True
    return not write and user.role == "ADMIN"


def get_session(
    session_id: int, project_id: int, user=None, write: bool = False
) -> ChatSession | None:
    """The session, only if it belongs to ``project_id`` and ``user`` may use it."""
    session = db.session.get(ChatSession, session_id)
    if session is None or session.project_id != project_id:
        return None
    if not can_use_session(user, session, write):
        return None
    return session


def resolve_or_create_session(
    session_id: int | None, project_id: int, user=None, write: bool = False
) -> ChatSession:
    """Return the session for session_id, or a sensible default.

    Used wherever a session_id is optional at the API layer: an unknown or
    omitted id - or one that belongs to another project or another user -
    falls back to the caller's most recently created session in the project,
    creating a brand new one only if they have none at all. ``user=None`` is
    an internal caller and resolves across every session of the project.
    """
    if session_id is not None:
        session = get_session(session_id, project_id, user, write)
        if session is not None:
            return session

    query = db.session.query(ChatSession).filter(ChatSession.project_id == project_id)
    if user is not None:
        query = query.filter(ChatSession.created_by_id == user.id)
    latest = query.order_by(ChatSession.created_at.desc(), ChatSession.id.desc()).first()
    if latest is not None:
        return latest
    return create_session(project_id, created_by_id=user.id if user is not None else None)


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


def list_sessions(project_id: int, user=None) -> list[dict]:
    """The sessions ``user`` started in the project, most recently active first."""
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
    query = (
        db.session.query(ChatSession, first_content.label("first_content"))
        .outerjoin(activity, activity.c.session_id == ChatSession.id)
        .filter(ChatSession.project_id == project_id)
    )
    if user is not None:
        query = query.filter(ChatSession.created_by_id == user.id)
    rows = query.order_by(last_activity.desc(), ChatSession.id.asc()).all()
    return [
        {
            "id": session.id,
            "title": _title_from_content(content),
            "created_at": session.created_at.isoformat() if session.created_at else None,
        }
        for session, content in rows
    ]
