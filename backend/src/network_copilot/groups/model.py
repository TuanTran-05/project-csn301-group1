from datetime import datetime, timezone

from ..extensions import db

teacher_groups = db.Table(
    "teacher_groups",
    db.Column(
        "teacher_id",
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "group_id",
        db.Integer,
        db.ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

student_groups = db.Table(
    "student_groups",
    db.Column(
        "student_id",
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "group_id",
        db.Integer,
        db.ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

group_devices = db.Table(
    "group_devices",
    db.Column(
        "group_id",
        db.Integer,
        db.ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "device_id",
        db.Integer,
        db.ForeignKey("devices.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Group(db.Model):
    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False, index=True)
    is_auto_approve = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    teachers = db.relationship(
        "User", secondary=teacher_groups, back_populates="teaching_groups"
    )
    students = db.relationship(
        "User", secondary=student_groups, back_populates="student_groups"
    )
    devices = db.relationship(
        "Device", secondary=group_devices, back_populates="groups"
    )
