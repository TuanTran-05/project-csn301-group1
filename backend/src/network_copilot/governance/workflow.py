import hashlib
import json
import re
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..audit.model import AuditLog
from ..changes.model import ChangeRequest
from ..devices.model import Device
from ..errors import ConflictError, ForbiddenError, InvalidStateError, NotFoundError, PolicyViolationError, ValidationError
from ..extensions import db
from ..groups.model import Group
from .access import actor, group_member, permitted, policy, require, role
from .model import DeviceLease, Notification, Reservation, now


def command_prefix(command, prefix):
    return command == prefix or command.startswith(prefix + " ")


def risk(commands, device):
    from ..commands.policy import analyze_risk
    p = policy()
    normalized = [" ".join(c.lower().split()) for c in commands]
    body = [c for c in normalized if c not in {"configure terminal", "conf t", "end", "exit"}]
    if any(command_prefix(c, r) for c in body for r in p.forbidden_commands):
        raise PolicyViolationError("A command is prohibited by the Administrator's policy.")
    if any(command_prefix(c, r) for c in body for r in p.high_risk_commands):
        return "high"
    if device.environment == "production":
        return "high"
    if device.environment == "physical_lab" and not all(any(command_prefix(c, r) for r in p.physical_lab_low_risk_commands) for c in body):
        return "high"
    if device.environment == "virtual" and not device.isolated:
        return "high"
    if device.role in {"core", "distribution", "isp", "firewall"}:
        return "high"
    # No user-provided risk overrides. Routing, ACL and management changes stay high.
    if any(re.match(r"^(?:no\s+)?(?:router|ip route|ip address|ip access|access-list|username|enable|line|snmp-server|aaa|crypto|interface (?:vlan|loopback))\b", c) for c in body):
        return "high"
    safe = (r"^hostname [a-z0-9-]+$", r"^vlan \d+$", r"^name [a-z0-9_-]+$",
            r"^interface (?:gigabitethernet|fastethernet|ethernet|gi|fa|eth)[\d/\.]+$",
            r"^description .+$", r"^switchport access vlan \d+$", r"^switchport mode access$")
    if not all(any(re.fullmatch(pattern, c) for pattern in safe) for c in body):
        return "high"
    return "low" if analyze_risk(commands, device.role) == "LOW" else "high"


def bind(change, user_id, device, group_id=None):
    user = actor(user_id)
    if role(user) != "ADMIN" and group_id is None:
        scopes = [g.id for g in device.groups if permitted(user, "config.create", g.id, device)]
        if len(scopes) != 1:
            raise ForbiddenError("Select exactly one authorized group for this change.")
        group_id = scopes[0]
    require(user, "config.create", group_id, device)
    if group_id is not None:
        group = db.session.get(Group, group_id)
        if not group or group.archived or device not in group.devices:
            raise ForbiddenError("Device is not allocated to this active group.")
    pending = db.session.query(ChangeRequest).filter(ChangeRequest.requested_by_id == user.id,
                                                    ChangeRequest.status.in_(["pending_approval", "approved", "running"])).count()
    if pending >= policy().pending_change_limit:
        raise ConflictError("Pending change quota reached.")
    change.group_id = group_id
    check_assignment(change)
    change.baseline_revision = device.baseline_revision
    change.risk_level = risk(change.commands, device)
    if change.requires_confirmation:
        change.risk_level = "high"
    if change.source == "restore":
        change.risk_level = "high"
        change.requires_confirmation = True
    if role(user) == "STUDENT" and change.risk_level == "high" and not (change.description or "").strip():
        raise ValidationError("High-risk requests must include their purpose in description.")
    from ..backups.model import ConfigBackup
    backup = db.session.query(ConfigBackup).filter_by(device_id=device.id).order_by(ConfigBackup.id.desc()).first()
    if backup:
        change.baseline_hash = config_hash(backup.running_config)


def config_hash(config):
    # Ignore volatile IOS header lines, preserve every actual configuration line.
    lines = [line.rstrip() for line in config.splitlines()
             if not re.match(r"^(Building configuration|Current configuration|! Last configuration change|! NVRAM config last updated)", line)]
    return hashlib.sha256("\n".join(lines).strip().encode()).hexdigest()


def digest(change):
    device = db.session.get(Device, change.device_id)
    p = policy()
    content = {key: getattr(change, key) for key in (
        "commands", "execution_mode", "source", "device_id", "group_id", "assignment_id", "target_hostname", "target_management_ip",
        "target_ssh_port", "target_device_type", "baseline_revision", "baseline_hash", "verification_commands",
        "verification_plan", "operation_expectations", "requires_confirmation", "risk_level")}
    content["environment"] = [device.environment, device.isolated, device.role]
    content["policy"] = [p.independent_approval, p.production_admin_only, p.approval_minutes, p.forbidden_commands,
                         p.high_risk_commands, p.physical_lab_low_risk_commands]
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def approve_allowed(change, user_id):
    user = actor(user_id)
    if role(user) != "ADMIN" and change.group_id is None:
        raise ForbiddenError("A non-Admin reviewer requires an explicit group scope.")
    if role(user) == "STUDENT":
        raise ForbiddenError("Students cannot approve changes.")
    device = db.session.get(Device, change.device_id)
    if device is None:
        raise NotFoundError("Approved target no longer exists.")
    require(user, "config.approve", change.group_id, device)
    if policy().independent_approval and role(user) != "ADMIN" and user.id == change.requested_by_id:
        raise ForbiddenError("Independent approval is required; send this change to Admin.")
    if device.environment == "production" and policy().production_admin_only and role(user) != "ADMIN":
        raise ForbiddenError("Production changes require Administrator approval.")
    require(actor(change.requested_by_id), "config.create", change.group_id, device)
    if device.baseline_revision != change.baseline_revision:
        raise InvalidStateError("Device baseline changed; create a new preview.")
    risk(change.commands, device)
    change.approval_digest = digest(change)


def deployment_allowed(change, user_id):
    user = actor(user_id)
    if role(user) != "ADMIN" and change.group_id is None:
        raise ForbiddenError("A non-Admin deployment requires an explicit group scope.")
    device = db.session.get(Device, change.device_id)
    if device is None:
        raise NotFoundError("Approved target no longer exists.")
    if (device.environment != "virtual" or not device.isolated) and not change.baseline_hash:
        raise InvalidStateError("Capture a device baseline before previewing a physical or shared-lab change.")
    require(user, "config.deploy", change.group_id, device)
    if change.source == "restore":
        require(user, "config.restore", change.group_id, device)
    require(actor(change.requested_by_id), "config.create", change.group_id, device)
    check_assignment(change)
    if role(user) == "STUDENT" and user.id != change.requested_by_id:
        raise ForbiddenError("Students may deploy only their own approved changes.")
    if change.group_id:
        group = db.session.get(Group, change.group_id)
        if not group or group.archived or device not in group.devices:
            raise ForbiddenError("Target group or allocation is no longer active.")
    if device.baseline_revision != change.baseline_revision or change.approval_digest != digest(change):
        raise InvalidStateError("Approval no longer matches this configuration, target, baseline or policy; preview and approve again.")
    if not change.approved_at or change.approved_at.replace(tzinfo=None) + timedelta(minutes=policy().approval_minutes) <= now():
        raise InvalidStateError("Approval expired; preview and approve again.")
    if change.approved_by_id:
        approve_allowed(change, change.approved_by_id)
    else:
        group = db.session.get(Group, change.group_id) if change.group_id else None
        if not group or not group.is_auto_approve or risk(change.commands, device) != "low" or change.requires_confirmation:
            raise ForbiddenError("Automatic approval is no longer permitted.")
    risk(change.commands, device)
    active = db.session.query(Reservation).filter(Reservation.device_id == device.id, Reservation.cancelled.is_(False),
                                                 Reservation.starts_at <= now(), Reservation.ends_at > now()).all()
    if any(r.maintenance for r in active):
        raise ForbiddenError("Device is in a maintenance window.")
    if device.environment != "virtual" and role(user) != "ADMIN":
        if not any(r.group_id == change.group_id and not r.maintenance for r in active):
            raise ForbiddenError("A current reservation for this group is required on physical devices.")
    if device.status == "offline":
        raise InvalidStateError("Device is offline.")


def check_assignment(change):
    if not change.assignment_id:
        return
    from .model import Assignment
    assignment = db.session.get(Assignment, change.assignment_id)
    if not assignment or assignment.group_id != change.group_id or assignment.device_id != change.device_id:
        raise ForbiddenError("Change must target its assignment's group and device.")
    if not assignment.starts_at <= now() < assignment.due_at:
        raise ForbiddenError("Assignment execution window is closed.")
    body = [c.strip().lower() for c in change.commands if c.strip().lower() not in {"configure terminal", "end", "exit"}]
    if assignment.allowed_prefixes and any(not any(command_prefix(c, p) for p in assignment.allowed_prefixes) for c in body):
        raise ForbiddenError("Change contains operations outside the assignment.")


def claim(change, user_id=None):
    try:
        db.session.add(DeviceLease(device_id=change.device_id, change_id=change.id))
        db.session.flush()
        result = db.session.execute(update(ChangeRequest).where(ChangeRequest.id == change.id, ChangeRequest.status == "approved")
                                    .values(status="running"))
        if result.rowcount != 1:
            raise ConflictError("Change already claimed or no longer approved.")
        db.session.add(AuditLog(action="change.deploy.started", result="success",
                               user_id=user_id or change.approved_by_id, device_id=change.device_id,
                               details={"change_id": change.id, "approval_digest": change.approval_digest}))
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ConflictError("Another deployment owns this device. Resolve its lease before retrying.") from exc
    except Exception:
        db.session.rollback()
        raise


def release(change):
    # Do not auto-expire leases: a slow/partitioned worker might still write to the router.
    db.session.query(DeviceLease).filter_by(device_id=change.device_id, change_id=change.id).delete()
    db.session.commit()


def notify_change(change, kind):
    recipients = {change.requested_by_id}
    if change.group_id:
        group = db.session.get(Group, change.group_id)
        if group:
            recipients.update(u.id for u in group.teachers)
    from ..auth.model import User
    recipients.update(u.id for u in db.session.query(User).filter_by(role="ADMIN"))
    for user_id in recipients - {None}:
        db.session.add(Notification(user_id=user_id, kind=kind, message=f"Change {change.id}: {change.status}."))
