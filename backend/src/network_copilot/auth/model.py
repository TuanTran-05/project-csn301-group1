from datetime import datetime, timezone
from enum import Enum

from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db


class Role(str, Enum):
    ADMIN = "ADMIN"
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"
    # Retained so existing accounts and deployments remain readable.
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"


ROLES = tuple(role.value for role in Role)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(
        db.Enum(
            Role,
            name="user_role",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda roles: [role.value for role in roles],
            length=16,
        ),
        nullable=False,
        default=Role.STUDENT,
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    teaching_groups = db.relationship(
        "Group", secondary="teacher_groups", back_populates="teachers"
    )
    student_groups = db.relationship(
        "Group", secondary="student_groups", back_populates="students"
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def to_dict(self) -> dict:
        """Public representation. Never includes the password hash."""
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role.value if isinstance(self.role, Role) else self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<User {self.username} ({self.role})>"
