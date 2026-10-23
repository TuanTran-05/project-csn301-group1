"""Deny by default. A grant is evaluated as one complete scope, never unioned."""
from datetime import datetime

from ..auth.model import User
from ..errors import ForbiddenError, NotFoundError, ValidationError
from ..extensions import db
from ..groups.model import Group
from .model import Grant, Policy, Suspension, now

ENVIRONMENTS = {"virtual", "physical_lab", "production"}
ACTIONS = {
    "student.create", "group.create", "device.read", "device.observe",
    "config.create", "config.validate", "config.simulate", "config.approve",
    "config.deploy", "config.restore", "ai.use", "lab.create", "lab.start",
    "lab.stop", "lab.reset", "lab.delete", "lab.template", "device.reserve",
    "assignment.manage",
}
STUDENT_FORBIDDEN = {"student.create", "group.create", "config.approve", "assignment.manage", "lab.template", "lab.delete"}
TEACHER_DEFAULT_ACTIONS = {"student.create", "group.create"}


def role(user):
    return user.role.value if hasattr(user.role, "value") else str(user.role)


def actor(user_id):
    user = db.session.get(User, user_id) if type(user_id) is int and user_id > 0 else None
    if user is None or not user.is_active:
        raise ForbiddenError("An active account is required.")
    return user


def policy():
    # Defaults are available even before the administrator edits the policy.
    record = db.session.get(Policy, 1)
    if record is None:
        record = Policy(id=1, independent_approval=True, production_admin_only=True,
                        approval_minutes=60, ai_daily_limit=100, lab_limit=5,
                        lab_runtime_minutes=120, pending_change_limit=20,
                        forbidden_commands=[], high_risk_commands=[], physical_lab_low_risk_commands=[])
    return record


def group_member(user, group):
    if not user or not user.is_active or group.archived:
        return False
    if role(user) == "ADMIN":
        return True
    members = group.teachers if role(user) == "TEACHER" else group.students
    return user in members and db.session.get(Suspension, (group.id, user.id)) is None


def require_group(user, group_id, manage=False):
    if type(group_id) is not int or group_id <= 0:
        raise ValidationError("A valid group_id is required.")
    group = db.session.get(Group, group_id)
    if group is None:
        raise NotFoundError("Group not found.")
    if role(user) == "ADMIN":
        return group
    if not group_member(user, group) or (manage and role(user) != "TEACHER"):
        raise ForbiddenError("This group is outside your scope.")
    return group


def live(grant, seen=None):
    seen = set() if seen is None else set(seen)
    if grant.id in seen or grant.revoked or grant.starts_at > now() or (grant.expires_at and grant.expires_at <= now()):
        return False
    seen.add(grant.id)
    subject = db.session.get(User, grant.user_id)
    issuer = db.session.get(User, grant.issuer_id)
    if not subject or not subject.is_active or not issuer or not issuer.is_active:
        return False
    if grant.group_id:
        group = db.session.get(Group, grant.group_id)
        if not group or not group_member(subject, group):
            return False
        if role(issuer) != "ADMIN" and (role(issuer) != "TEACHER" or not group_member(issuer, group)):
            return False
    if grant.parent_id:
        parent = db.session.get(Grant, grant.parent_id)
        return bool(parent and parent.user_id == issuer.id and parent.delegable
                    and parent.action == grant.action and live(parent, seen)
                    and covers(parent, grant.group_id, grant.device_id, grant.environment)
                    and (not parent.expires_at or (grant.expires_at and grant.expires_at <= parent.expires_at)))
    return role(issuer) == "ADMIN"


def covers(grant, group_id, device_id, environment):
    return ((grant.group_id is None or grant.group_id == group_id)
            and (grant.device_id is None or grant.device_id == device_id)
            and (grant.environment is None or grant.environment == environment))


def permitted(user, action, group_id=None, device=None, environment=None):
    if not user or not user.is_active or action not in ACTIONS:
        return False
    if role(user) == "ADMIN":
        return True
    if role(user) == "OPERATOR" and action == "device.read" and device is not None:
        # Legacy Operators use project ownership/sharing rather than the
        # Teacher/Student group-grant system. Preserve that project boundary
        # when listing or resolving devices, including before a project-scoped
        # edit such as deleting one.
        from ..projects.model import Project
        from ..projects.service import access_level

        project = db.session.get(Project, device.project_id)
        return project is not None and access_level(user, project) is not None
    if role(user) not in {"TEACHER", "STUDENT"}:
        return False
    if role(user) == "TEACHER" and action in TEACHER_DEFAULT_ACTIONS:
        return True
    if role(user) == "STUDENT" and action in STUDENT_FORBIDDEN:
        return False
    if group_id is not None:
        group = db.session.get(Group, group_id)
        if not group or not group_member(user, group) or (device and device not in group.devices):
            return False
        scopes = [group_id]
    elif device:
        scopes = [g.id for g in device.groups if group_member(user, g)]
        if not scopes:
            return False
    else:
        scopes = [None]
    environment = device.environment if device else environment
    for grant in db.session.query(Grant).filter_by(user_id=user.id, action=action):
        if live(grant) and any(covers(grant, scope, device.id if device else None, environment) for scope in scopes):
            return True
    return False


def require(user, action, group_id=None, device=None, environment=None):
    if not permitted(user, action, group_id, device, environment):
        raise ForbiddenError(f"Missing active permission: {action} in this scope.")


def timestamp(value, field="timestamp"):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        from datetime import timezone
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (AttributeError, TypeError, ValueError):
        raise ValidationError(f"{field} must be an ISO-8601 timestamp with timezone.")


def issue_grant(issuer, data):
    subject = actor(data.get("user_id"))
    action = data.get("action")
    if not isinstance(action, str) or action not in ACTIONS or role(subject) not in {"TEACHER", "STUDENT"}:
        raise ValidationError("A known action and Teacher or Student recipient are required.")
    if role(subject) == "STUDENT" and action in STUDENT_FORBIDDEN:
        raise ForbiddenError("This action cannot be granted to Students.")
    if data.get("environment") not in ENVIRONMENTS | {None}:
        raise ValidationError("Unknown environment.")
    group_id, device_id = data.get("group_id"), data.get("device_id")
    if group_id:
        group = require_group(issuer, group_id, manage=True)
        if not group_member(subject, group):
            raise ForbiddenError("Recipient must belong to the target group.")
    if device_id:
        from ..devices.model import Device
        device = db.session.get(Device, device_id)
        if not device or not group_id or device not in group.devices:
            raise ValidationError("A device grant must name its allocated group.")
        if data.get("environment") and data["environment"] != device.environment:
            raise ValidationError("Environment does not match device.")
    expires = timestamp(data["expires_at"], "expires_at") if data.get("expires_at") else None
    starts = timestamp(data["starts_at"], "starts_at") if data.get("starts_at") else now()
    if expires and expires <= max(starts, now()):
        raise ValidationError("Expiry must be later than start and current time.")
    delegable = data.get("delegable", False)
    if not isinstance(delegable, bool):
        raise ValidationError("delegable must be boolean.")
    parent = None
    if role(issuer) != "ADMIN":
        if role(issuer) != "TEACHER" or role(subject) != "STUDENT" or not group_id or delegable:
            raise ForbiddenError("Teachers may grant non-delegable permissions to their Students only.")
        parent = next((g for g in db.session.query(Grant).filter_by(user_id=issuer.id, action=action)
                       if g.delegable and live(g) and covers(g, group_id, device_id, data.get("environment"))
                       and (not g.expires_at or (expires and expires <= g.expires_at))
                       and starts >= g.starts_at), None)
        if parent is None:
            raise ForbiddenError("The requested grant exceeds your delegation authority.")
    grant = Grant(user_id=subject.id, issuer_id=issuer.id, parent_id=parent.id if parent else None,
                  action=action, group_id=group_id, device_id=device_id, environment=data.get("environment"),
                  starts_at=starts, expires_at=expires, delegable=delegable)
    db.session.add(grant)
    return grant
