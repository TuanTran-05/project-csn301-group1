from datetime import datetime, timezone

from ..extensions import db


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Grant(db.Model):
    __tablename__ = "permission_grants"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    issuer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("permission_grants.id"))
    action = db.Column(db.String(64), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"))
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"))
    environment = db.Column(db.String(32))
    starts_at = db.Column(db.DateTime, nullable=False, default=now)
    expires_at = db.Column(db.DateTime)
    delegable = db.Column(db.Boolean, nullable=False, default=False)
    revoked = db.Column(db.Boolean, nullable=False, default=False)


class Suspension(db.Model):
    __tablename__ = "group_suspensions"
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)


class Policy(db.Model):
    __tablename__ = "platform_policy"
    id = db.Column(db.Integer, primary_key=True, default=1)
    independent_approval = db.Column(db.Boolean, nullable=False, default=True)
    production_admin_only = db.Column(db.Boolean, nullable=False, default=True)
    approval_minutes = db.Column(db.Integer, nullable=False, default=60)
    ai_daily_limit = db.Column(db.Integer, nullable=False, default=100)
    lab_limit = db.Column(db.Integer, nullable=False, default=5)
    lab_runtime_minutes = db.Column(db.Integer, nullable=False, default=120)
    pending_change_limit = db.Column(db.Integer, nullable=False, default=20)
    forbidden_commands = db.Column(db.JSON, nullable=False, default=list)
    high_risk_commands = db.Column(db.JSON, nullable=False, default=list)
    physical_lab_low_risk_commands = db.Column(db.JSON, nullable=False, default=list)


class Invitation(db.Model):
    __tablename__ = "group_invitations"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked = db.Column(db.Boolean, nullable=False, default=False)


class JoinRequest(db.Model):
    __tablename__ = "group_join_requests"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending")
    __table_args__ = (db.UniqueConstraint("group_id", "user_id"),)


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(64), nullable=False)
    message = db.Column(db.String(512), nullable=False)
    read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now)


class Reservation(db.Model):
    __tablename__ = "device_reservations"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=False, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    maintenance = db.Column(db.Boolean, nullable=False, default=False)
    cancelled = db.Column(db.Boolean, nullable=False, default=False)


class DeviceLease(db.Model):
    __tablename__ = "device_leases"
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), primary_key=True)
    change_id = db.Column(db.Integer, db.ForeignKey("change_requests.id"), nullable=False)
    acquired_at = db.Column(db.DateTime, nullable=False, default=now)


class Draft(db.Model):
    __tablename__ = "config_drafts"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)


class DraftVersion(db.Model):
    __tablename__ = "config_draft_versions"
    id = db.Column(db.Integer, primary_key=True)
    draft_id = db.Column(db.Integer, db.ForeignKey("config_drafts.id"), nullable=False)
    commands = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now)


class Assignment(db.Model):
    __tablename__ = "assignments"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    instructions = db.Column(db.Text, nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False, default=now)
    due_at = db.Column(db.DateTime, nullable=False)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=False)
    allowed_prefixes = db.Column(db.JSON, nullable=False, default=list)


class Submission(db.Model):
    __tablename__ = "assignment_submissions"
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    version_id = db.Column(db.Integer, db.ForeignKey("config_draft_versions.id"), nullable=False)
    feedback = db.Column(db.Text)
    score = db.Column(db.Float)
    submitted_at = db.Column(db.DateTime, nullable=False, default=now)


class LabTemplate(db.Model):
    __tablename__ = "lab_templates"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    topology = db.Column(db.JSON, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    allowed_teacher_ids = db.Column(db.JSON, nullable=False, default=list, server_default="[]")


class ConfigTemplate(db.Model):
    __tablename__ = "ai_config_templates"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    instructions = db.Column(db.Text, nullable=False)
    commands = db.Column(db.JSON, nullable=False, default=list)
    enabled = db.Column(db.Boolean, nullable=False, default=True)


class Lab(db.Model):
    __tablename__ = "labs"
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("lab_templates.id"), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="created")
    external_id = db.Column(db.String(255))
    expires_at = db.Column(db.DateTime, nullable=False)


class Usage(db.Model):
    __tablename__ = "daily_usage"
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    day = db.Column(db.String(10), primary_key=True)
    ai_calls = db.Column(db.Integer, nullable=False, default=0)


def public(row):
    result = {}
    for column in row.__table__.columns:
        if column.name == "token_hash":
            continue
        value = getattr(row, column.name)
        result[column.name] = value.replace(tzinfo=timezone.utc).isoformat() if isinstance(value, datetime) else value
    return result
