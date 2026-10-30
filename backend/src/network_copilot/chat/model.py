"""Shared team chat: named sessions, each holding an ordered transcript."""

from datetime import datetime, timezone

from ..extensions import db, in_check

CHAT_ROLES = ("user", "assistant", "system")


def utc_isoformat(value: datetime | None) -> str | None:
    """Serialize stored UTC timestamps with an explicit timezone marker.

    SQL databases commonly return ``DateTime(timezone=False)`` values as naive
    datetimes even though this app writes them in UTC. Without the marker,
    browsers interpret the timestamp as local time and display it several
    hours off.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


class ChatSession(db.Model):
    """A named conversation thread shared by the members of one project.

    ``created_by_id`` records who started the session; it does not restrict
    which project members can read or continue it. The title is derived from
    the session's first message by chat/session_service.py.
    """

    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True)
    # Sessions are project-scoped and shared with every project member.
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ChatSession #{self.id}>"


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    username = db.Column(db.String(64))
    role = db.Column(db.String(16), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    payload = db.Column(db.JSON)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        in_check("role", CHAT_ROLES, "role_valid"),
        db.Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "content": self.content,
            "payload": self.payload,
            "created_at": utc_isoformat(self.created_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ChatMessage {self.role} #{self.id}>"
