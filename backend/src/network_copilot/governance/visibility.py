"""Request-scoped visibility; worker/scheduler inventory reads are internal."""
from flask import has_request_context
from flask_jwt_extended import get_jwt_identity

from ..auth.service import current_user
from ..errors import ForbiddenError
from ..extensions import db
from .access import permitted, role


def request_user():
    if not has_request_context():
        return None
    try:
        return current_user() if get_jwt_identity() else None
    except RuntimeError:
        return None


def devices(records, user=None):
    user = user or request_user()
    return records if user is None else [d for d in records if permitted(user, "device.read", device=d)]


def can_read_change(user, change):
    if role(user) == "ADMIN":
        return True
    if role(user) in {"OPERATOR", "VIEWER"}:
        from ..projects.model import Project
        from ..projects.service import access_level

        project = db.session.get(Project, change.device.project_id) if change.device else None
        if project is not None and access_level(user, project) is not None:
            return True
    if (
        getattr(change, "ai_management_approved", False)
        and change.requested_by_id == user.id
        and role(user) in {"OPERATOR", "VIEWER"}
    ):
        from ..devices.model import Device
        from ..projects.model import Project
        device = db.session.get(Device, change.device_id)
        project = db.session.get(Project, device.project_id) if device else None
        if (
            project
            and project.environment == "pnetlab"
            and device.environment == "pnetlab"
            and device.isolated
            and device.deployable
        ):
            return True
    if change.group_id is None and change.requested_by_id != user.id:
        return False
    if not permitted(user, "device.read", change.group_id, change.device):
        return False
    return role(user) == "TEACHER" or change.requested_by_id == user.id


def changes(records):
    user = request_user()
    return records if user is None else [c for c in records if can_read_change(user, c)]


def check_change(change):
    user = request_user()
    if user and not can_read_change(user, change):
        raise ForbiddenError("Change is outside your scope.")


def batches(records):
    user = request_user()
    if user is None or role(user) == "ADMIN":
        return records
    # all() of no changes is True: an empty batch is only its requester's.
    return [b for b in records
            if (all(can_read_change(user, c) for c in b.changes) if b.changes
                else b.requested_by_id == user.id)]


def events(records):
    user = request_user()
    if user is None or role(user) == "ADMIN":
        return records
    from ..devices.model import Device
    from ..changes.model import ChangeRequest, ChangeBatch
    from ..groups.model import Group
    from ..extensions import db
    visible = []
    for record in records:
        if record.user_id == user.id:
            visible.append(record)
            continue
        if role(user) != "TEACHER":
            continue
        details = getattr(record, "details", {}) or {}
        if not isinstance(details, dict):
            continue
        change = db.session.get(ChangeRequest, details["change_id"]) if type(details.get("change_id")) is int else None
        if change and can_read_change(user, change):
            visible.append(record)
            continue
        batch = db.session.get(ChangeBatch, details["batch_id"]) if type(details.get("batch_id")) is int else None
        if batch and all(can_read_change(user, c) for c in batch.changes):
            visible.append(record)
            continue
        group_id = getattr(record, "group_id", None) or details.get("group_id")
        if type(group_id) is int and record.device_id and permitted(user, "device.read", group_id, db.session.get(Device, record.device_id)):
            visible.append(record)
    return visible
