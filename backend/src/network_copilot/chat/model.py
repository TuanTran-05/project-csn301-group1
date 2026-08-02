"""Shared team chat: named sessions, each holding an ordered transcript."""

from datetime import datetime, timezone

from ..extensions import db

CHAT_ROLES = ("user", "assistant", "system")


class ChatSession(db.Model):
    """A named conversation thread within the shared team chat.

    Sessions are shared across the whole team, not private per user: any
    authenticated user can see and switch to any session. created_by_id
    only records who started it - it does not restrict who can read it.
    There is no stored title: it is derived from the session's first
    message by chat/session_service.py, computed at read time.
    """

    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True)
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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "content": self.content,
            "payload": self.payload,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ChatMessage {self.role} #{self.id}>"
