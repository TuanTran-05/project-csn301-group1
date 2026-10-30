"""Project-shared chat sessions and transcript access.

Every member who can access a project can read and continue any chat session
in that project. Project membership remains the isolation boundary.
"""

from ..extensions import db
from .model import ChatMessage, ChatSession, utc_isoformat

_TITLE_MAX_LENGTH = 60


def create_session(project_id: int, created_by_id: int | None = None) -> ChatSession:
    session = ChatSession(project_id=project_id, created_by_id=created_by_id)
    db.session.add(session)
    db.session.commit()
    return session


def can_use_session(user, session: ChatSession, write: bool = False) -> bool:
    """Whether ``user`` may read or post to a session in an accessible project.

    ``user=None`` is an internal caller. Reads and writes are shared among all
    active members of the same project.
    """
    if user is None:
        return True
    if not user.is_active:
        return False
    from ..projects.model import Project
    from ..projects.service import access_level

    project = db.session.get(Project, session.project_id)
    return project is not None and access_level(user, project) is not None


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

    An unknown id or one belonging to another project falls back to the most
    recently active session in this project, creating one only if necessary.
    All project members resolve against the same shared session list.
    """
    if session_id is not None:
        session = get_session(session_id, project_id, user, write)
        if session is not None:
            return session

    query = db.session.query(ChatSession).filter(ChatSession.project_id == project_id)
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
        "created_at": utc_isoformat(session.created_at),
    }


def list_sessions(project_id: int, user=None) -> list[dict]:
    """All sessions in the project, most recently active first."""
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
    rows = query.order_by(last_activity.desc(), ChatSession.id.asc()).all()
    return [
        {
            "id": session.id,
            "title": _title_from_content(content),
            "created_at": utc_isoformat(session.created_at),
        }
        for session, content in rows
    ]
