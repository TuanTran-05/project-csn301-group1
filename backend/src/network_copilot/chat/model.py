"""Shared team chat transcript with the AI copilot."""

from datetime import datetime, timezone

from ..extensions import db

CHAT_ROLES = ("user", "assistant", "system")


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
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
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "content": self.content,
            "payload": self.payload,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ChatMessage {self.role} #{self.id}>"
