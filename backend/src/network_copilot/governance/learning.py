"""Versioned student work, assessment, lab lifecycle and restoration requests."""
import difflib
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import update

from ..auth.service import current_user, roles_required
from ..changes import service as changes
from ..changes.model import ChangeRequest
from ..devices.model import Device
from ..errors import AppError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from ..extensions import db
from .access import actor, permitted, policy, require, require_group, role, timestamp
from .model import Assignment, ConfigTemplate, Draft, DraftVersion, Lab, LabTemplate, Submission, now, public
from .routes import boolean, notify, payload, save, text_field
from .workflow import command_prefix

bp = Blueprint("learning", __name__, url_prefix="/api")


def get(model, identity):
    record = db.session.get(model, identity) if type(identity) is int else None
    if record is None:
        raise NotFoundError(f"{model.__name__} not found.")
    return record


def command_list(data):
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands or len(commands) > 1000 or any(not isinstance(c, str) or len(c) > 1000 for c in commands):
        raise ValidationError("commands must contain 1–1000 command strings.")
    return commands


def own_draft(draft_id):
    record = get(Draft, draft_id)
    user = current_user()
    require_group(user, record.group_id)
    if role(user) != "ADMIN" and record.owner_id != user.id:
        raise ForbiddenError("Only the author may access a private draft.")
    require(user, "config.create", record.group_id, get(Device, record.device_id))
    return record


@bp.get("/drafts")
@jwt_required()
def drafts():
    user = current_user()
    records = db.session.query(Draft).filter_by(owner_id=user.id).all()
    return jsonify(items=[public(d) for d in records if permitted(user, "config.create", d.group_id, get(Device, d.device_id))])


@bp.post("/drafts")
@jwt_required()
def create_draft():
    user, data = current_user(), payload()
    device = get(Device, data.get("device_id"))
    require_group(user, data.get("group_id"))
    require(user, "config.create", data["group_id"], device)
    commands = command_list(data)
    record = Draft(owner_id=user.id, group_id=data["group_id"], device_id=device.id, title=text_field(data, "title"))
    db.session.add(record)
    db.session.flush()
    version = DraftVersion(draft_id=record.id, commands=commands)
    db.session.add(version)
    save("draft.create", {"draft_id": record.id})
    return jsonify(draft=public(record), version=public(version)), 201


@bp.post("/drafts/<int:draft_id>/versions")
@jwt_required()
def version_draft(draft_id):
    own_draft(draft_id)
    version = DraftVersion(draft_id=draft_id, commands=command_list(payload()))
    db.session.add(version)
    save("draft.version", {"draft_id": draft_id})
    return jsonify(public(version)), 201


@bp.get("/drafts/<int:draft_id>/versions")
@jwt_required()
def draft_versions(draft_id):
    own_draft(draft_id)
    return jsonify(items=[public(v) for v in db.session.query(DraftVersion).filter_by(draft_id=draft_id).order_by(DraftVersion.id)])


@bp.get("/drafts/<int:draft_id>/diff")
@jwt_required()
def diff_draft(draft_id):
    own_draft(draft_id)
    left, right = get(DraftVersion, request.args.get("from", type=int)), get(DraftVersion, request.args.get("to", type=int))
    if left.draft_id != draft_id or right.draft_id != draft_id:
        raise ForbiddenError("Both versions must belong to the selected draft.")
    return jsonify(diff=list(difflib.unified_diff(left.commands, right.commands, fromfile=str(left.id), tofile=str(right.id), lineterm="")))


@bp.post("/drafts/<int:draft_id>/validate")
@jwt_required()
def validate_draft(draft_id):
    draft, data = own_draft(draft_id), payload()
    version, device = get(DraftVersion, data.get("version_id")), get(Device, draft.device_id)
    if version.draft_id != draft.id:
        raise ForbiddenError("Version does not belong to this draft.")
    require(current_user(), "config.validate", draft.group_id, device)
    change = changes.prepare_change(current_user().id, device, version.commands, group_id=draft.group_id)
    return jsonify(valid=True, risk_level=change.risk_level, warnings=change.warnings,
                   verification_plan=change.verification_plan, capability_tier=change.capability_tier,
                   message="Backend policy validation only; vendor syntax and impact require an isolated simulation.")


@bp.post("/drafts/<int:draft_id>/preview")
@jwt_required()
def preview_draft(draft_id):
    draft, data = own_draft(draft_id), payload()
    version = get(DraftVersion, data.get("version_id"))
    if version.draft_id != draft.id:
        raise ForbiddenError("Version does not belong to this draft.")
    result = changes.create_preview(current_user().id, draft.device_id, version.commands,
                                    description=draft.title, group_id=draft.group_id, assignment_id=data.get("assignment_id"))
    return jsonify(result.to_dict()), 201


@bp.post("/drafts/<int:draft_id>/simulate")
@jwt_required()
def simulate_draft(draft_id):
    draft, data = own_draft(draft_id), payload()
    version, device = get(DraftVersion, data.get("version_id")), get(Device, draft.device_id)
    if version.draft_id != draft.id:
        raise ForbiddenError("Version does not belong to this draft.")
    require(current_user(), "config.simulate", draft.group_id, device)
    changes.prepare_change(current_user().id, device, version.commands, group_id=draft.group_id)
    simulator = current_app.config.get("ISOLATED_SIMULATOR")
    if simulator is None:
        raise Unavailable("An isolated simulation provider has not been configured.")
    # Deliberately supply no production connection information or device credentials.
    result = simulator(device_type=device.device_type, commands=list(version.commands))
    save("draft.simulate", {"draft_id": draft.id, "version_id": version.id})
    return jsonify(result=result)


@bp.get("/groups/<int:group_id>/assignments")
@jwt_required()
def assignments(group_id):
    require_group(current_user(), group_id)
    return jsonify(items=[public(a) for a in db.session.query(Assignment).filter_by(group_id=group_id)])


@bp.post("/groups/<int:group_id>/assignments")
@jwt_required()
def create_assignment(group_id):
    user, data = current_user(), payload()
    require_group(user, group_id, manage=True)
    device = get(Device, data.get("device_id"))
    require(user, "assignment.manage", group_id, device)
    starts = timestamp(data["starts_at"]) if data.get("starts_at") else now()
    due = timestamp(data.get("due_at"))
    if due <= max(starts, now()):
        raise ValidationError("Due date must be in the future and after start.")
    prefixes = data.get("allowed_prefixes", [])
    if not isinstance(prefixes, list) or any(not isinstance(p, str) or not p.strip() for p in prefixes):
        raise ValidationError("allowed_prefixes must be a list of nonempty strings.")
    assignment = Assignment(group_id=group_id, author_id=user.id, device_id=device.id,
                            title=text_field(data, "title"), instructions=text_field(data, "instructions", 20000),
                            starts_at=starts, due_at=due, allowed_prefixes=[p.strip().lower() for p in prefixes])
    db.session.add(assignment)
    save("assignment.create", {"group_id": group_id})
    return jsonify(public(assignment)), 201


@bp.post("/assignments/<int:assignment_id>/submissions")
@roles_required("STUDENT")
def submit(assignment_id):
    user, data = current_user(), payload()
    assignment = get(Assignment, assignment_id)
    require_group(user, assignment.group_id)
    if not assignment.starts_at <= now() < assignment.due_at:
        raise ForbiddenError("Assignment is outside its submission window.")
    version = get(DraftVersion, data.get("version_id"))
    draft = own_draft(version.draft_id)
    if draft.group_id != assignment.group_id or draft.device_id != assignment.device_id:
        raise ForbiddenError("Submission must target the assignment's group and device.")
    if assignment.allowed_prefixes and any(not any(command_prefix(c.strip().lower(), p) for p in assignment.allowed_prefixes)
                                           for c in version.commands if c.strip().lower() not in {"configure terminal", "end", "exit"}):
        raise ForbiddenError("Submission contains an operation outside the assignment.")
    record = Submission(assignment_id=assignment_id, student_id=user.id, version_id=version.id)
    db.session.add(record)
    notify(assignment.author_id, "assignment.submitted", f"New submission for assignment {assignment.id}.")
    save("assignment.submit", {"assignment_id": assignment_id, "version_id": version.id})
    return jsonify(public(record)), 201


@bp.get("/assignments/<int:assignment_id>/submissions")
@jwt_required()
def submissions(assignment_id):
    user, assignment = current_user(), get(Assignment, assignment_id)
    require_group(user, assignment.group_id)
    records = db.session.query(Submission).filter_by(assignment_id=assignment_id)
    if role(user) == "STUDENT":
        records = records.filter_by(student_id=user.id)
    else:
        require(user, "assignment.manage", assignment.group_id, get(Device, assignment.device_id))
    return jsonify(items=[dict(public(s), commands=get(DraftVersion, s.version_id).commands) for s in records])


@bp.patch("/submissions/<int:submission_id>/grade")
@jwt_required()
def grade(submission_id):
    user, data = current_user(), payload()
    record = get(Submission, submission_id)
    assignment = get(Assignment, record.assignment_id)
    require_group(user, assignment.group_id, manage=True)
    require(user, "assignment.manage", assignment.group_id, get(Device, assignment.device_id))
    score = data.get("score")
    if type(score) not in {float, int} or not 0 <= score <= 100:
        raise ValidationError("score must be between 0 and 100.")
    record.score, record.feedback = score, text_field(data, "feedback", 20000)
    notify(record.student_id, "assignment.graded", f"Submission {record.id} has been graded.")
    save("assignment.grade", {"submission_id": record.id, "score": score})
    return jsonify(public(record))


class Unavailable(AppError):
    status_code = 503
    error = "provider_not_configured"


def driver():
    provider = current_app.config.get("LAB_DRIVER")
    if provider is None:
        raise Unavailable("No lab runtime provider is configured. No infrastructure action was performed.")
    return provider


@bp.get("/lab-templates")
@jwt_required()
def templates():
    require(current_user(), "lab.template")
    return jsonify(items=[public(t) for t in db.session.query(LabTemplate).filter_by(enabled=True)
                          if role(current_user()) == "ADMIN" or current_user().id in t.allowed_teacher_ids])


@bp.post("/lab-templates")
@roles_required("ADMIN")
def create_template():
    data = payload()
    if not isinstance(data.get("topology"), dict):
        raise ValidationError("topology must be an object.")
    allowed = data.get("allowed_teacher_ids", [])
    if not isinstance(allowed, list) or any(type(i) is not int or role(actor(i)) != "TEACHER" for i in allowed):
        raise ValidationError("allowed_teacher_ids must name active Teachers.")
    template = LabTemplate(name=text_field(data, "name", 100), topology=data["topology"], allowed_teacher_ids=allowed)
    db.session.add(template)
    save("lab.template.create", {"name": template.name})
    return jsonify(public(template)), 201


@bp.patch("/lab-templates/<int:template_id>")
@roles_required("ADMIN")
def disable_template(template_id):
    template = get(LabTemplate, template_id)
    data = payload()
    if "enabled" in data:
        template.enabled = boolean(data, "enabled")
    if "allowed_teacher_ids" in data:
        allowed = data["allowed_teacher_ids"]
        if not isinstance(allowed, list) or any(type(i) is not int or role(actor(i)) != "TEACHER" for i in allowed):
            raise ValidationError("allowed_teacher_ids must name active Teachers.")
        template.allowed_teacher_ids = allowed
    save("lab.template.update", {"template_id": template.id, "enabled": template.enabled})
    return jsonify(public(template))


@bp.get("/labs")
@jwt_required()
def labs():
    user = current_user()
    records = db.session.query(Lab).all()
    return jsonify(items=[public(l) for l in records if role(user) == "ADMIN" or
                          (l.owner_id == user.id and permitted(user, "lab.start", l.group_id, environment="virtual")) or
                          (role(user) == "TEACHER" and permitted(user, "lab.create", l.group_id, environment="virtual"))])


@bp.post("/labs")
@jwt_required()
def create_lab():
    user, data = current_user(), payload()
    group = require_group(user, data.get("group_id"), manage=True)
    require(user, "lab.create", group.id, environment="virtual")
    require(user, "lab.template")
    template = get(LabTemplate, data.get("template_id"))
    if not template.enabled:
        raise ForbiddenError("Template disabled.")
    if role(user) != "ADMIN" and user.id not in template.allowed_teacher_ids:
        raise ForbiddenError("This template has not been allocated to you.")
    owner = actor(data.get("owner_id", user.id))
    require_group(owner, group.id)
    provider = driver()
    from ..auth.model import User
    db.session.query(User).filter_by(id=owner.id).update({"token_version": User.token_version})
    count = db.session.query(Lab).filter(Lab.owner_id == owner.id, Lab.status != "deleted").count()
    if count >= policy().lab_limit:
        raise ConflictError("Lab quota reached.")
    lab = Lab(template_id=template.id, group_id=group.id, owner_id=owner.id,
              expires_at=now() + timedelta(minutes=policy().lab_runtime_minutes), status="provisioning")
    db.session.add(lab)
    save("lab.provision.request", {"group_id": group.id})
    try:
        external_id = provider.provision(template.topology, deadline=lab.expires_at, idempotency_key=f"lab-{lab.id}")
        if not isinstance(external_id, str) or not external_id or len(external_id) > 255:
            raise ValueError("Provider did not return a valid external lab identifier.")
        lab.external_id = external_id
        lab.status = "stopped"
    except Exception:
        lab.status = "failed"
        save("lab.provision.failed", {"lab_id": lab.id})
        raise Unavailable("Lab provisioning failed. Check provider logs before retrying.")
    save("lab.provision.complete", {"lab_id": lab.id})
    return jsonify(public(lab)), 201


@bp.post("/labs/<int:lab_id>/<action>")
@jwt_required()
def lab_action(lab_id, action):
    if action not in {"start", "stop", "reset", "delete"}:
        raise NotFoundError("Unknown lab action.")
    user, lab = current_user(), get(Lab, lab_id)
    require_group(user, lab.group_id)
    require(user, "lab." + action, lab.group_id, environment="virtual")
    if role(user) == "STUDENT" and lab.owner_id != user.id:
        raise ForbiddenError("Students may operate only their own lab.")
    if action in {"start", "reset"} and now() >= lab.expires_at:
        raise ForbiddenError("Lab runtime allocation expired.")
    provider = driver()
    if lab.status not in {"running", "stopped"}:
        raise ConflictError("Lab is not ready for this action.")
    claimed = db.session.execute(update(Lab).where(Lab.id == lab.id, Lab.status.in_(["running", "stopped"])).values(status="busy"))
    if claimed.rowcount != 1:
        db.session.rollback()
        raise ConflictError("Another operation owns this lab.")
    save("lab.action.request", {"lab_id": lab.id, "action": action})
    try:
        provider.operate(lab.external_id, action, deadline=lab.expires_at)
        lab.status = {"start": "running", "stop": "stopped", "reset": "stopped", "delete": "deleted"}[action]
    except Exception:
        lab.status = "failed"
        save("lab.action.failed", {"lab_id": lab.id, "action": action})
        raise Unavailable("Lab action failed; reconcile provider state before retrying.")
    save("lab.action.complete", {"lab_id": lab.id, "action": action})
    return jsonify(public(lab))


@bp.post("/changes/<int:change_id>/reject")
@jwt_required()
def reject(change_id):
    from .workflow import approve_allowed, notify_change
    record = get(ChangeRequest, change_id)
    changes._reject_batch_child(record)
    if record.status != "pending_approval":
        raise ConflictError("Only pending changes can be rejected.")
    approve_allowed(record, current_user().id)
    record.rejection_reason = text_field(payload(), "reason", 512)
    record.status = "rejected"
    record.approval_digest = None
    notify_change(record, "change.rejected")
    save("change.reject", {"change_id": record.id, "reason": record.rejection_reason})
    return jsonify(record.to_dict())


@bp.post("/changes/<int:change_id>/escalate")
@jwt_required()
def escalate(change_id):
    from ..auth.model import User
    record = get(ChangeRequest, change_id)
    require_group(current_user(), record.group_id, manage=True)
    if record.status != "pending_approval":
        raise ConflictError("Only pending changes can be escalated.")
    reason = text_field(payload(), "reason", 512)
    for admin in db.session.query(User).filter_by(role="ADMIN"):
        notify(admin.id, "change.escalated", f"Change {record.id} requires review: {reason}")
    save("change.escalate", {"change_id": record.id, "reason": reason})
    return jsonify(message="Administrator notified; configuration remains pending approval.")


@bp.post("/changes/<int:change_id>/restore-request")
@jwt_required()
def restore_request(change_id):
    from ..auth.model import User
    record = get(ChangeRequest, change_id)
    require(current_user(), "device.read", record.group_id, get(Device, record.device_id))
    reason = text_field(payload(), "reason", 512)
    for admin in db.session.query(User).filter_by(role="ADMIN"):
        notify(admin.id, "restore.requested", f"Restore requested for change {record.id}: {reason}")
    save("restore.request", {"change_id": record.id, "reason": reason})
    return jsonify(message="Restoration requested. Review device-specific recovery instructions; no rollback was executed."), 202


@bp.post("/changes/<int:change_id>/restore-preview")
@jwt_required()
def restore_preview(change_id):
    original, data, user = get(ChangeRequest, change_id), payload(), current_user()
    device = get(Device, original.device_id)
    require(user, "config.restore", original.group_id, device)
    if original.status not in {"success", "failed"}:
        raise ConflictError("Only completed or failed changes can be restored.")
    if not device.recovery_instructions:
        raise ConflictError("Administrator must document this device's recovery procedure first.")
    # Inverse commands are deliberately supplied/reviewed by the operator. A generic
    # 'no ...' inverse is not a safe restoration of an arbitrary previous state.
    result = changes.create_preview(user.id, device.id, command_list(data),
                                    description=text_field(data, "description"), source="restore",
                                    execution_mode=data.get("execution_mode", "config"), group_id=original.group_id)
    save("restore.preview", {"original_change_id": original.id, "change_id": result.id})
    return jsonify(result.to_dict()), 201


@bp.get("/ai-templates")
@jwt_required()
def ai_templates():
    require(current_user(), "ai.use")
    return jsonify(items=[public(t) for t in db.session.query(ConfigTemplate).filter_by(enabled=True)])


@bp.post("/ai-templates")
@roles_required("ADMIN")
def create_ai_template():
    data = payload()
    record = ConfigTemplate(name=text_field(data, "name", 100), instructions=text_field(data, "instructions", 10000), commands=command_list(data))
    from .redaction import redact_output
    if redact_output(record.instructions) != record.instructions or redact_output(record.commands) != record.commands:
        raise ValidationError("AI templates must not contain credentials.")
    db.session.add(record)
    save("ai.template.create", {"name": record.name})
    return jsonify(public(record)), 201


@bp.patch("/ai-templates/<int:template_id>")
@roles_required("ADMIN")
def update_ai_template(template_id):
    data, record = payload(), get(ConfigTemplate, template_id)
    if "enabled" in data:
        record.enabled = boolean(data, "enabled")
    if "instructions" in data:
        record.instructions = text_field(data, "instructions", 10000)
    if "commands" in data:
        record.commands = command_list(data)
    from .redaction import redact_output
    if redact_output(record.instructions) != record.instructions or redact_output(record.commands) != record.commands:
        db.session.rollback()
        raise ValidationError("AI templates must not contain credentials.")
    save("ai.template.update", {"template_id": record.id})
    return jsonify(public(record))
