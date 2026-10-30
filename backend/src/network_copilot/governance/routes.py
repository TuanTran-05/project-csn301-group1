import hashlib
import secrets
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy.exc import IntegrityError

from ..audit.model import AuditLog
from ..audit.service import redact_sensitive
from ..auth.model import Role, User
from ..auth.service import current_user, roles_required
from ..devices.model import Device
from ..errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ..extensions import db
from ..groups.model import Group
from .access import actor, issue_grant, live, permitted, policy, require, require_group, role, timestamp
from .model import Grant, Invitation, JoinRequest, Notification, Policy, Reservation, Suspension, now, public

bp = Blueprint("governance", __name__, url_prefix="/api")


def payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("A JSON object is required.")
    for key, value in data.items():
        if key.endswith("_id") and (type(value) is not int or value <= 0):
            raise ValidationError(f"{key} must be a positive integer.")
    return data


def text_field(data, key, maximum=255):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError(f"{key} must be nonempty text of at most {maximum} characters.")
    return value.strip()


def boolean(data, key):
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValidationError(f"{key} must be boolean.")
    return value


def save(action, details=None):
    """Security mutations and their audit record commit together."""
    user = current_user()
    db.session.add(AuditLog(action=action, result="success", user_id=user.id,
                           details=redact_sensitive(details or {})))
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ConflictError("This operation conflicts with an existing record.") from exc


def notify(user_id, kind, message):
    db.session.add(Notification(user_id=user_id, kind=kind, message=message[:512]))


def group_json(group):
    return {"id": group.id, "name": group.name, "archived": group.archived,
            "is_auto_approve": group.is_auto_approve, "teacher_ids": [u.id for u in group.teachers],
            "student_ids": [u.id for u in group.students], "device_ids": [d.id for d in group.devices]}


@bp.get("/users")
@roles_required("ADMIN")
def users():
    return jsonify(items=[u.to_dict() for u in db.session.query(User).order_by(User.id)])


@bp.post("/users")
@jwt_required()
def create_user():
    user, data = current_user(), payload()
    desired = data.get("role", "STUDENT")
    if not isinstance(desired, str) or desired not in {"TEACHER", "STUDENT"}:
        raise ValidationError("Only Teacher and Student accounts can be created through this API.")
    if role(user) != "ADMIN":
        if role(user) != "TEACHER" or desired != "STUDENT":
            raise ForbiddenError("Teachers may only create Students.")
        require(user, "student.create")
    password = text_field(data, "password", 128)
    if len(password) < 12:
        raise ValidationError("Password must contain at least 12 characters.")
    record = User(username=text_field(data, "username", 64), role=Role(desired))
    record.set_password(password)
    db.session.add(record)
    save("user.create", {"username": record.username, "role": desired})
    return jsonify(record.to_dict()), 201


@bp.patch("/users/<int:user_id>")
@roles_required("ADMIN")
def update_user(user_id):
    record = db.session.get(User, user_id)
    if not record:
        raise NotFoundError("User not found.")
    data = payload()
    if set(data) - {"is_active", "display_name"}:
        raise ValidationError("Only is_active and display_name can be changed.")
    if "is_active" in data:
        enabled = boolean(data, "is_active")
        if role(record) == "ADMIN" and not enabled:
            raise ForbiddenError("The sole Administrator cannot be disabled.")
        record.is_active = enabled
        record.token_version += 1
    if "display_name" in data:
        record.display_name = text_field(data, "display_name", 100)
    save("user.update", {"user_id": user_id, "changes": data})
    return jsonify(record.to_dict())


@bp.post("/admin/transfer")
@roles_required("ADMIN")
def transfer_admin():
    data, previous = payload(), current_user()
    target = actor(data.get("user_id"))
    if target.id == previous.id or role(target) != "TEACHER":
        raise ValidationError("Choose an active Teacher.")
    if not previous.check_password(data.get("password", "")):
        raise ForbiddenError("Current password is required.")
    previous.role = Role.TEACHER
    previous.token_version += 1
    db.session.flush()
    target.role = Role.ADMIN
    target.token_version += 1
    # Root grants remain platform-owned when administrator ownership changes.
    db.session.query(Grant).filter_by(issuer_id=previous.id, parent_id=None).update({"issuer_id": target.id})
    save("admin.transfer", {"previous_id": previous.id, "new_id": target.id})
    return jsonify(target.to_dict())


@bp.patch("/auth/profile")
@jwt_required()
def profile():
    user, data = current_user(), payload()
    user.display_name = text_field(data, "display_name", 100)
    save("auth.profile")
    return jsonify(user.to_dict())


@bp.post("/auth/password")
@jwt_required()
def password():
    user, data = current_user(), payload()
    if not user.check_password(data.get("current_password", "")):
        raise ForbiddenError("Current password is incorrect.")
    replacement = text_field(data, "new_password", 128)
    if len(replacement) < 12:
        raise ValidationError("Password must contain at least 12 characters.")
    user.set_password(replacement)
    user.token_version += 1
    save("auth.password")
    return jsonify(message="Password changed; sign in again.")


@bp.get("/permissions")
@jwt_required()
def permissions():
    user = current_user()
    records = db.session.query(Grant)
    if role(user) != "ADMIN":
        records = records.filter((Grant.user_id == user.id) | (Grant.issuer_id == user.id))
    return jsonify(items=[dict(public(g), active=live(g)) for g in records])


@bp.post("/permissions")
@jwt_required()
def grant_permission():
    record = issue_grant(current_user(), payload())
    notify(record.user_id, "permission.granted", f"Permission {record.action} granted.")
    save("permission.grant", {"recipient_id": record.user_id, "action": record.action, "group_id": record.group_id})
    return jsonify(public(record)), 201


@bp.delete("/permissions/<int:grant_id>")
@jwt_required()
def revoke_permission(grant_id):
    grant = db.session.get(Grant, grant_id)
    if grant is None:
        raise NotFoundError("Grant not found.")
    user = current_user()
    if role(user) != "ADMIN":
        if grant.issuer_id != user.id or role(user) != "TEACHER":
            raise ForbiddenError("Only the issuer or Administrator may revoke this grant.")
        require_group(user, grant.group_id, manage=True)
    grant.revoked = True
    save("permission.revoke", {"grant_id": grant_id})
    return "", 204


@bp.get("/groups")
@jwt_required()
def groups():
    user = current_user()
    records = db.session.query(Group).all() if role(user) == "ADMIN" else (user.teaching_groups if role(user) == "TEACHER" else user.student_groups)
    return jsonify(items=[group_json(g) for g in records])


@bp.post("/groups")
@jwt_required()
def create_group():
    user, data = current_user(), payload()
    require(user, "group.create")
    group = Group(name=text_field(data, "name", 100))
    if role(user) == "TEACHER":
        group.teachers.append(user)
    db.session.add(group)
    save("group.create", {"name": group.name})
    return jsonify(group_json(group)), 201


@bp.patch("/groups/<int:group_id>")
@jwt_required()
def update_group(group_id):
    group, data = require_group(current_user(), group_id, manage=True), payload()
    if set(data) - {"name", "archived"}:
        raise ValidationError("Only name and archived can be changed.")
    if "name" in data:
        group.name = text_field(data, "name", 100)
    if "archived" in data:
        group.archived = boolean(data, "archived")
    save("group.update", {"group_id": group_id, "changes": data})
    return jsonify(group_json(group))


@bp.post("/groups/<int:group_id>/transfer")
@roles_required("ADMIN")
def transfer_group(group_id):
    group, data = require_group(current_user(), group_id), payload()
    teacher = actor(data.get("teacher_id"))
    if role(teacher) != "TEACHER":
        raise ValidationError("Target must be an active Teacher.")
    group.teachers = [teacher]
    save("group.transfer", {"group_id": group_id, "teacher_id": teacher.id})
    return jsonify(group_json(group))


@bp.put("/groups/<int:group_id>/devices/<int:device_id>")
@roles_required("ADMIN")
def allocate_device(group_id, device_id):
    group = require_group(current_user(), group_id)
    device = db.session.get(Device, device_id)
    if not device:
        raise NotFoundError("Device not found.")
    if device not in group.devices:
        group.devices.append(device)
    save("device.allocate", {"group_id": group_id, "device_id": device_id})
    return jsonify(group_json(group))


@bp.delete("/groups/<int:group_id>/devices/<int:device_id>")
@roles_required("ADMIN")
def deallocate_device(group_id, device_id):
    group = require_group(current_user(), group_id)
    group.devices = [d for d in group.devices if d.id != device_id]
    db.session.query(Grant).filter_by(group_id=group_id, device_id=device_id).update({"revoked": True})
    save("device.deallocate", {"group_id": group_id, "device_id": device_id})
    return "", 204


@bp.put("/groups/<int:group_id>/members/<int:user_id>")
@jwt_required()
def add_member(group_id, user_id):
    group = require_group(current_user(), group_id, manage=True)
    member = actor(user_id)
    if role(member) != "STUDENT":
        raise ValidationError("Group members must be Students.")
    if member not in group.students:
        group.students.append(member)
    notify(user_id, "group.member", f"Added to group {group.name}.")
    save("group.member.add", {"group_id": group_id, "user_id": user_id})
    return jsonify(group_json(group))


@bp.delete("/groups/<int:group_id>/members/<int:user_id>")
@jwt_required()
def remove_member(group_id, user_id):
    user = current_user()
    group = require_group(user, group_id, manage=user.id != user_id)
    group.students = [s for s in group.students if s.id != user_id]
    db.session.query(Grant).filter_by(group_id=group_id, user_id=user_id).update({"revoked": True})
    save("group.member.remove", {"group_id": group_id, "user_id": user_id})
    return "", 204


@bp.patch("/groups/<int:group_id>/members/<int:user_id>")
@jwt_required()
def suspend_member(group_id, user_id):
    group, data = require_group(current_user(), group_id, manage=True), payload()
    if not any(u.id == user_id for u in group.students):
        raise NotFoundError("Student not in this group.")
    record = db.session.get(Suspension, (group_id, user_id))
    if boolean(data, "suspended") and record is None:
        db.session.add(Suspension(group_id=group_id, user_id=user_id))
    elif not data["suspended"] and record:
        db.session.delete(record)
    save("group.member.suspend", {"group_id": group_id, "user_id": user_id, "suspended": data["suspended"]})
    return jsonify(suspended=data["suspended"])


@bp.post("/groups/<int:group_id>/invitations")
@jwt_required()
def invite(group_id):
    require_group(current_user(), group_id, manage=True)
    token = secrets.token_urlsafe(24)
    record = Invitation(group_id=group_id, token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=now() + timedelta(days=7))
    db.session.add(record)
    save("group.invitation.create", {"group_id": group_id})
    return jsonify(id=record.id, code=token, expires_at=record.expires_at.isoformat() + "Z"), 201


@bp.delete("/invitations/<int:invitation_id>")
@jwt_required()
def revoke_invitation(invitation_id):
    invitation = db.session.get(Invitation, invitation_id)
    if not invitation:
        raise NotFoundError("Invitation not found.")
    require_group(current_user(), invitation.group_id, manage=True)
    invitation.revoked = True
    save("group.invitation.revoke", {"invitation_id": invitation_id})
    return "", 204


@bp.post("/groups/join")
@roles_required("STUDENT")
def join():
    code = text_field(payload(), "code", 100)
    invitation = db.session.query(Invitation).filter_by(token_hash=hashlib.sha256(code.encode()).hexdigest(), revoked=False).first()
    if not invitation or invitation.expires_at <= now():
        raise NotFoundError("Invitation expired or not found.")
    group = db.session.get(Group, invitation.group_id)
    if group.archived:
        raise ForbiddenError("Group is archived.")
    record = db.session.query(JoinRequest).filter_by(group_id=group.id, user_id=current_user().id).first()
    if record is None:
        record = JoinRequest(group_id=group.id, user_id=current_user().id)
        db.session.add(record)
    elif record.status != "pending":
        record.status = "pending"
    for teacher in group.teachers:
        notify(teacher.id, "group.join", f"Join request for group {group.name}.")
    save("group.join.request", {"group_id": group.id})
    return jsonify(public(record)), 201


@bp.get("/groups/<int:group_id>/join-requests")
@jwt_required()
def join_requests(group_id):
    require_group(current_user(), group_id, manage=True)
    return jsonify(items=[public(r) for r in db.session.query(JoinRequest).filter_by(group_id=group_id)])


@bp.post("/join-requests/<int:join_id>/review")
@jwt_required()
def review_join(join_id):
    record = db.session.get(JoinRequest, join_id)
    if not record:
        raise NotFoundError("Join request not found.")
    group = require_group(current_user(), record.group_id, manage=True)
    if record.status != "pending":
        raise ConflictError("Join request already reviewed.")
    accepted = boolean(payload(), "accepted")
    member = actor(record.user_id)
    if accepted and member not in group.students:
        group.students.append(member)
    record.status = "accepted" if accepted else "rejected"
    notify(member.id, "group.join.review", f"Join request {record.status}.")
    save("group.join.review", {"join_id": join_id, "status": record.status})
    return jsonify(public(record))


@bp.get("/policy")
@jwt_required()
def get_policy():
    return jsonify(public(policy()))


@bp.patch("/policy")
@roles_required("ADMIN")
def update_policy():
    data, record = payload(), policy()
    fields = {c.name for c in Policy.__table__.columns} - {"id"}
    if set(data) - fields:
        raise ValidationError("Unknown policy field.")
    for key, value in data.items():
        if key in {"independent_approval", "production_admin_only"}:
            boolean(data, key)
        elif key.endswith("commands"):
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in value):
                raise ValidationError("Command rules must be a list of literal command prefixes.")
            value = [v.strip().lower() for v in value]
        elif type(value) is not int or not 1 <= value <= 100000:
            raise ValidationError("Policy limits must be positive integers up to 100000.")
        setattr(record, key, value)
    db.session.add(record)
    save("policy.update", data)
    return jsonify(public(record))


@bp.get("/notifications")
@jwt_required()
def notifications():
    user = current_user()
    items = [public(n) for n in db.session.query(Notification).filter_by(user_id=user.id).order_by(Notification.id.desc()).limit(100)]
    expiring = [public(g) for g in db.session.query(Grant).filter_by(user_id=user.id)
                if live(g) and g.expires_at and g.expires_at <= now() + timedelta(days=1)]
    return jsonify(items=items, expiring_permissions=expiring)


@bp.post("/notifications/<int:notification_id>/read")
@jwt_required()
def read_notification(notification_id):
    item = db.session.get(Notification, notification_id)
    if not item or item.user_id != current_user().id:
        raise NotFoundError("Notification not found.")
    item.read = True
    db.session.commit()
    return jsonify(public(item))


@bp.get("/reservations")
@jwt_required()
def reservations():
    user = current_user()
    records = db.session.query(Reservation).all()
    return jsonify(items=[public(r) for r in records if role(user) == "ADMIN" or r.user_id == user.id
                          or (r.group_id and any(g.id == r.group_id for g in user.teaching_groups))])


@bp.post("/reservations")
@jwt_required()
def reserve():
    user, data = current_user(), payload()
    device = db.session.get(Device, data.get("device_id"))
    if not device:
        raise NotFoundError("Device not found.")
    maintenance = data.get("maintenance", False)
    if not isinstance(maintenance, bool):
        raise ValidationError("maintenance must be boolean.")
    if maintenance and role(user) != "ADMIN":
        raise ForbiddenError("Only Admin may schedule maintenance.")
    group_id = data.get("group_id")
    if not maintenance:
        require_group(user, group_id, manage=True)
        require(user, "device.reserve", group_id, device)
    start, end = timestamp(data.get("starts_at")), timestamp(data.get("ends_at"))
    if start >= end or end <= now():
        raise ValidationError("Reservation must have a future end after its start.")
    # Serialize scheduling on the device row, including SQLite (write lock).
    db.session.query(Device).filter_by(id=device.id).update({"baseline_revision": Device.baseline_revision})
    overlap = db.session.query(Reservation).filter(Reservation.device_id == device.id, Reservation.cancelled.is_(False),
                                                 Reservation.starts_at < end, Reservation.ends_at > start).first()
    if overlap:
        db.session.rollback()
        raise ConflictError("Device already reserved in this interval.")
    record = Reservation(device_id=device.id, group_id=group_id, user_id=user.id, starts_at=start, ends_at=end, maintenance=maintenance)
    db.session.add(record)
    save("reservation.create", {"device_id": device.id, "group_id": group_id})
    return jsonify(public(record)), 201


@bp.delete("/reservations/<int:reservation_id>")
@jwt_required()
def cancel_reservation(reservation_id):
    record = db.session.get(Reservation, reservation_id)
    if not record:
        raise NotFoundError("Reservation not found.")
    user = current_user()
    if role(user) != "ADMIN":
        require_group(user, record.group_id, manage=True)
    record.cancelled = True
    save("reservation.cancel", {"reservation_id": record.id})
    return "", 204


@bp.post("/devices/<int:device_id>/baseline")
@jwt_required()
def capture_baseline(device_id):
    from ..backups.service import capture_backup
    from .model import DeviceLease
    device = db.session.get(Device, device_id)
    if device is None:
        raise NotFoundError("Device not found.")
    user = current_user()
    if role(user) == "OPERATOR":
        from ..projects.model import Project
        from ..projects.service import access_level

        project = db.session.get(Project, device.project_id)
        if project is None or access_level(user, project) not in {"owner", "editor"}:
            raise ForbiddenError("Project edit access is required to capture a device baseline.")
    else:
        require(user, "device.observe", device=device)
    if role(user) == "STUDENT":
        raise ForbiddenError("Ask your Teacher to capture the baseline.")
    if db.session.get(DeviceLease, device_id):
        raise ConflictError("Device has an active deployment.")
    backup = capture_backup(device, reason="approval_baseline")
    save("device.baseline", {"device_id": device.id, "backup_id": backup.id})
    return jsonify(backup.to_dict()), 201


@bp.put("/devices/<int:device_id>/credentials")
@roles_required("ADMIN")
def set_credentials(device_id):
    from ..credentials.service import store_device_credential
    from .model import DeviceLease
    device = db.session.get(Device, device_id)
    if device is None:
        raise NotFoundError("Device not found.")
    if db.session.get(DeviceLease, device_id):
        raise ConflictError("Device has an active deployment.")
    data = payload()
    username = text_field(data, "username", 64)
    password = data.get("password")
    enable_secret = data.get("enable_secret")
    if not isinstance(password, str) or not password or len(password) > 1000:
        raise ValidationError("A nonempty device password is required.")
    if enable_secret is not None and (not isinstance(enable_secret, str) or len(enable_secret) > 1000):
        raise ValidationError("enable_secret must be text or null.")
    store_device_credential(device_id, username, password, enable_secret, commit=False)
    save("device.credentials.update", {"device_id": device_id})
    return jsonify(device_id=device_id, configured=True)


@bp.get("/operations/leases")
@roles_required("ADMIN")
def device_leases():
    from .model import DeviceLease
    return jsonify(items=[public(item) for item in db.session.query(DeviceLease)])


@bp.post("/operations/leases/<int:device_id>/release")
@roles_required("ADMIN")
def release_device_lease(device_id):
    from .model import DeviceLease
    from ..changes.model import ChangeRequest
    lease = db.session.get(DeviceLease, device_id)
    if not lease:
        raise NotFoundError("Lease not found.")
    data = payload()
    if data.get("confirm_worker_stopped") is not True:
        raise ValidationError("Confirm the old worker has stopped before releasing its device lease.")
    reason = text_field(data, "reason", 512)
    change = db.session.get(ChangeRequest, lease.change_id)
    if change and change.status == "running":
        change.status = "failed"
        change.error_message = "Administrator released orphaned lease: " + reason[:400]
    db.session.delete(lease)
    save("device.lease.release", {"device_id": device_id, "reason": reason})
    return jsonify(message="Lease released. Inspect the device and capture a fresh baseline before retrying.")
