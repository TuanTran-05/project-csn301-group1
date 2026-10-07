"""Projects: the isolation boundary for devices, topology, chat and changes.

Every device belongs to exactly one project. A user sees the projects they
own plus the ones another user shared with them; an ADMIN sees every
project. Nothing reaches a device without first resolving a project the
caller may access (see projects/scope.py).
"""

from datetime import datetime, timezone

from ..extensions import db

PROJECT_ENVIRONMENTS = ("pnetlab", "physical", "mixed")
MEMBER_ACCESS_LEVELS = ("viewer", "editor")


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255))
    # The CIDR every device in this project must have its management IP in.
    # Declared per project so labs and real networks can use their own ranges.
    management_network = db.Column(db.String(43), nullable=False)
    environment = db.Column(db.String(16), nullable=False, default="pnetlab")
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    members = db.relationship(
        "ProjectMember", back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "management_network": self.management_network,
            "environment": self.environment,
            "owner_id": self.owner_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Project {self.id} {self.name}>"


class ProjectMember(db.Model):
    """A user a project was shared with."""

    __tablename__ = "project_members"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    access = db.Column(db.String(16), nullable=False, default="viewer")
    granted_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    project = db.relationship("Project", back_populates="members")
    user = db.relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        db.UniqueConstraint("project_id", "user_id", name="uq_project_members_user"),
    )

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.user.username if self.user else None,
            "access": self.access,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
