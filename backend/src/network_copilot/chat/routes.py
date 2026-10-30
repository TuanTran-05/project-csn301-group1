import io
import re
import zipfile

from flask import Blueprint, jsonify, request, send_file
from flask_jwt_extended import jwt_required

from ..auth.service import current_user
from ..audit.service import record_event
from ..errors import ConflictError, ForbiddenError, InvalidStateError, NotFoundError, ValidationError
from ..extensions import db, limiter
from ..projects.scope import current_project
from .model import ChatMessage, ChatSession
from . import service, session_service

bp = Blueprint("chat", __name__, url_prefix="/api/chat")


@bp.get("/messages")
@jwt_required()
def list_messages():
    limit = request.args.get("limit", default=200, type=int)
    session_id = request.args.get("session_id", type=int)
    before_id = request.args.get("before_id", type=int)
    after_id = request.args.get("after_id", type=int)
    project = current_project()
    messages = service.list_messages(
        session_id=session_id,
        limit=limit,
        project_id=project.id,
        before_id=before_id,
        after_id=after_id,
        user=current_user(),
    )
    return jsonify({"items": [message.to_dict() for message in messages]}), 200


@bp.get("/sessions")
@jwt_required()
def list_sessions():
    project = current_project()
    return jsonify(
        {"items": session_service.list_sessions(project.id, current_user())}
    ), 200


@bp.post("/sessions")
@jwt_required()
def create_session():
    project = current_project()
    user = current_user()
    session = session_service.create_session(
        project.id, created_by_id=user.id if user else None
    )
    return jsonify(session_service.session_to_dict(session)), 201


@bp.post("/messages/<int:message_id>/config-files")
@jwt_required()
def export_message_configurations(message_id: int):
    """Package the configurations in one assistant reply for manual lab import."""
    project = current_project(need="editor")
    message = _chat_advice(message_id, project.id, current_user())
    if message is None:
        raise NotFoundError("The chat suggestion was not found in this project.")
    approval = request.get_json(silent=True)
    if not isinstance(approval, dict) or approval.get("approved") is not True:
        raise ValidationError("Confirm the suggested configurations before downloading.")

    from ..ai.schemas import DeviceConfiguration

    raw_configs = (message.payload or {}).get("configurations", [])
    if not isinstance(raw_configs, list) or not raw_configs:
        raise ValidationError("This chat answer does not contain device configurations.")
    try:
        configurations = [DeviceConfiguration.model_validate(item) for item in raw_configs]
    except Exception as exc:
        raise ValidationError("The saved device configurations are invalid.") from exc

    archive = io.BytesIO()
    filenames: set[str] = set()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "README.txt",
            "AI network configuration suggestions\n"
            "\nReview every file and confirm the device model, interfaces, VLANs, IP plan, and topology before importing it into PNETLab.\n"
            "The files mirror the chat suggestion. If AI Management was enabled, check the batch status in chat for the live apply result; this archive alone is not an execution report. For manual import, use the matching lab device and PNETLab's supported configuration workflow.\n"
            "The archive does not include credentials.\n",
        )
        for config in configurations:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", config.hostname).strip("._")
            if not safe_name:
                raise ValidationError("A device hostname cannot be used as a configuration filename.")
            filename = f"{safe_name}.cfg"
            if filename.casefold() in filenames:
                raise ValidationError("Device hostnames collide as configuration filenames.")
            filenames.add(filename.casefold())
            body = (
                f"! AI suggestion for {config.hostname} ({config.device_type})\n"
                "! Review before importing into PNETLab.\n"
                + "\n".join(config.commands)
                + "\n"
            )
            bundle.writestr(filename, body)

    archive.seek(0)
    user = current_user()
    record_event(
        action="chat.config_export",
        result="success",
        user_id=user.id if user else None,
        project_id=project.id,
        details={"message_id": message.id, "devices": sorted(filenames)},
    )
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"network-config-{message.id}.zip",
    )


def _chat_advice(message_id: int, project_id: int, user, write: bool = False):
    """An assistant reply in one of the current project's shared sessions."""
    message = (
        db.session.query(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .filter(
            ChatMessage.id == message_id,
            ChatMessage.role == "assistant",
            ChatSession.project_id == project_id,
        )
        .one_or_none()
    )
    if message is None or not session_service.can_use_session(
        user, db.session.get(ChatSession, message.session_id), write
    ):
        return None
    return message


def _require_ai_management_operator():
    """The project and user for an AI Management apply.

    An ADMIN or an OPERATOR with edit rights on the project may push an AI
    suggestion to devices. A VIEWER is read-only everywhere.
    """
    project = current_project(need="editor")
    user = current_user()
    if user is None or user.role not in {"ADMIN", "OPERATOR"}:
        raise ForbiddenError("Only an Admin or project Operator can approve an AI chat suggestion here.")
    return project, user


def _approved_chat_configs(message, project):
    from ..ai.schemas import DeviceConfiguration
    from ..devices.model import Device

    raw_configs = (message.payload or {}).get("configurations", [])
    if not isinstance(raw_configs, list) or not raw_configs:
        raise ValidationError("This chat answer does not contain device configurations.")
    try:
        configs = [DeviceConfiguration.model_validate(item) for item in raw_configs]
    except Exception as exc:
        raise ValidationError("The saved device configurations are invalid.") from exc

    project_devices = db.session.query(Device).filter_by(project_id=project.id).all()
    devices = {item.hostname.casefold(): item for item in project_devices}
    if len(devices) != len(project_devices):
        raise ConflictError("Device hostnames in this project are ambiguous for AI Management.")
    targets = []
    for config in configs:
        device = devices.get(config.hostname.casefold())
        if device is None:
            raise ValidationError(
                f"{config.hostname} is not an imported device in this project."
            )
        if (
            device.environment != "pnetlab"
            or device.device_type != config.device_type
            or not device.deployable
            or not device.isolated
            or device.status == "offline"
            or getattr(device, "credential", None) is None
        ):
            raise ValidationError(
                f"{device.hostname} must be an isolated PNETLab device with the "
                "matching type, a management IP and saved SSH credentials."
            )
        targets.append((device, config))
    return targets


@bp.post("/messages/<int:message_id>/apply-configurations")
@jwt_required()
@limiter.limit("10 per minute")
def apply_chat_configurations(message_id: int):
    """Apply one explicitly approved AI suggestion through the change pipeline."""
    project, user = _require_ai_management_operator()
    if project.environment != "pnetlab" or not project.ai_management_enabled:
        raise InvalidStateError(
            "The project owner must enable AI Management for this PNETLab project first."
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("approved") is not True:
        raise ValidationError("Explicitly approve this chat suggestion before applying it.")

    message = _chat_advice(message_id, project.id, user, write=True)
    if message is None:
        raise NotFoundError("The chat suggestion was not found in this project.")

    from ..changes.batch_service import BatchOperation, approve_batch, apply_batch, create_batch_preview
    from ..changes.model import ChangeBatch

    existing = (
        db.session.query(ChangeBatch)
        .filter_by(project_id=project.id, chat_message_id=message.id)
        .one_or_none()
    )
    if existing is not None:
        if existing.requested_by_id != user.id:
            raise ConflictError("Another project member already submitted this suggestion for PNETLab.")
        return jsonify({
            "batch": existing.to_dict(),
            "needs_apply": existing.status == "approved",
            "confirmation_required": existing.status == "approved" and existing.requires_confirmation,
        }), 200

    targets = _approved_chat_configs(message, project)
    from ..backups.service import capture_backup
    for device, _ in targets:
        # Freeze the current state only after the user approves this exact
        # response. Apply takes another backup and refuses if the device
        # changed between this point and the actual write.
        capture_backup(device, reason="ai_management_baseline")
    operations = [
        BatchOperation(
            device_hostnames=[device.hostname],
            execution_mode="config",
            commands=config.commands,
            verification_commands=[],
        )
        for device, config in targets
    ]
    user = current_user()
    batch = create_batch_preview(
        user.id if user else None,
        operations,
        description=f"AI chat suggestion #{message.id}",
        source="ai",
        project_id=project.id,
        chat_message_id=message.id,
        ai_management_approved=True,
    )
    batch = approve_batch(batch.id, user.id)
    confirmation_required = bool(batch.requires_confirmation)
    if not confirmation_required:
        batch = apply_batch(batch.id, user.id)
    record_event(
        action="chat.config_apply_approved",
        result="confirmation_required" if confirmation_required else batch.status,
        user_id=user.id,
        project_id=project.id,
        details={"message_id": message.id, "batch_id": batch.id},
    )
    return jsonify(
        {
            "batch": batch.to_dict(),
            "needs_apply": confirmation_required,
            "confirmation_required": confirmation_required,
        }
    ), 201


@bp.post("/messages/<int:message_id>/apply-design")
@jwt_required()
@limiter.limit("10 per minute")
def apply_chat_design(message_id: int):
    """Make an AI design proposal from the chat the project's L2/L3 design.

    The current diagram is saved as an automatic version first, so the
    previous design can be restored from the versions panel. Device
    configuration still goes through Preview -> Approve -> Apply.
    """
    from ..ai import design_assistant
    from ..projects.scope import write_project
    from ..topology import versions
    from ..topology.design import list_design

    project = write_project()
    message = _chat_advice(message_id, project.id, current_user(), write=True)
    payload = (message.payload or {}) if message is not None else {}
    if payload.get("flow") != "design_proposal" or not isinstance(payload.get("design"), dict):
        raise NotFoundError("The design proposal was not found in this project.")
    if payload.get("applied"):
        raise ConflictError("This design proposal was already applied.")

    user = current_user()
    version = versions._store(project, user, f"Trước thiết kế AI #{message.id}", None, auto=True)
    design_assistant.apply(project, payload["design"], payload.get("addressing_pool"))
    message.payload = {**payload, "applied": True, "version_id": version.id}
    db.session.commit()
    record_event(
        action="chat.design_applied",
        result="success",
        user_id=user.id if user else None,
        project_id=project.id,
        details={"message_id": message.id, "version_id": version.id},
    )
    return jsonify({"design": list_design(project), "version_id": version.id,
                    "payload": message.payload}), 200


@bp.post("/batches/<int:batch_id>/apply")
@jwt_required()
@limiter.limit("10 per minute")
def apply_approved_chat_batch(batch_id: int):
    """Finish an AI Management apply that requested a stronger phrase."""
    project, user = _require_ai_management_operator()
    if project.environment != "pnetlab" or not project.ai_management_enabled:
        raise InvalidStateError("AI Management is not enabled for this project.")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("approved") is not True:
        raise ValidationError("Confirm this AI Management apply before sending it.")

    from ..changes import batch_service

    batch = batch_service.get_batch(batch_id, project.id)
    if (
        batch.chat_message_id is None
        or not batch.changes
        or not all(change.ai_management_approved for change in batch.changes)
        or batch.requested_by_id != user.id
    ):
        raise NotFoundError("This batch is not an AI Management suggestion for this user.")
    message = _chat_advice(batch.chat_message_id, project.id, user, write=True)
    if message is None:
        raise NotFoundError("The original chat suggestion no longer exists.")
    _approved_chat_configs(message, project)
    batch = batch_service.apply_batch(
        batch_id,
        user.id,
        confirmation=payload.get("confirmation"),
    )
    return jsonify({"batch": batch.to_dict(), "confirmation_required": False}), 200


@bp.post("/batches/<int:batch_id>/cancel")
@jwt_required()
@limiter.limit("10 per minute")
def cancel_approved_chat_batch(batch_id: int):
    project, user = _require_ai_management_operator()
    from ..changes import batch_service

    batch = batch_service.get_batch(batch_id, project.id)
    if (
        batch.chat_message_id is None
        or not batch.changes
        or not all(change.ai_management_approved for change in batch.changes)
        or batch.requested_by_id != user.id
    ):
        raise NotFoundError("This batch is not an AI Management suggestion for this user.")
    batch = batch_service.cancel_batch(batch_id, user.id)
    return jsonify({"batch": batch.to_dict()}), 200
