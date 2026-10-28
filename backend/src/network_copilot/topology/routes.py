from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from pydantic import ValidationError as PydanticValidationError

from ..audit.service import record_event
from ..auth.service import current_user
from ..chat.model import ChatMessage, ChatSession
from ..errors import AppError
from ..extensions import db, limiter
from ..projects import service as project_service
from ..projects.model import Project
from . import blueprints, bootstrap, bootstrap_jobs, configgen, design, ipam, lint, notes, versions, discovery, pnetlab_import, service
from . import chat_import
from .schemas import ConfigRequestSchema
from .templates import DEVICE_TEMPLATES

# Topology routes name the project in the URL, unlike the inventory routes
# that use the X-Project-Id header: the diagram is addressed as a sub-resource
# of its project.
bp = Blueprint("topology", __name__, url_prefix="/api/projects/<int:project_id>/topology")


def _project(project_id: int, need: str):
    from flask import g

    project = project_service.require_project(current_user(), project_id, need)
    g.project_id = project.id
    return project


@bp.get("/ai-suggestion")
@jwt_required()
def latest_ai_suggestion(project_id: int):
    """Read the latest diagram suggested in a project chat session."""
    project = _project(project_id, "viewer")
    session_id = request.args.get("session_id", type=int)
    messages = (
        db.session.query(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .filter(ChatSession.project_id == project.id, ChatMessage.role == "assistant")
    )

    searches = (
        [messages.filter(ChatSession.id == session_id)]
        if session_id is not None
        else []
    )
    searches.append(messages)
    for search in searches:
        for message in search.order_by(ChatMessage.id.desc()).limit(500):
            payload = message.payload if isinstance(message.payload, dict) else {}
            raw_diagram = payload.get("diagram")
            if not isinstance(raw_diagram, dict):
                continue
            try:
                diagram, _ = chat_import.complete_diagram_addresses(project, raw_diagram)
            except (PydanticValidationError, AppError):
                continue
            if diagram != raw_diagram:
                updated_payload = dict(payload)
                updated_payload["diagram"] = diagram
                message.payload = updated_payload
                db.session.commit()
            imported = chat_import.was_imported(project, message)
            if imported:
                chat_import.mark_imported(project, message)
                db.session.commit()
            return jsonify({
                "suggestion": {
                    "message_id": message.id,
                    "session_id": message.session_id,
                    "explanation": message.content,
                    "diagram": diagram,
                    "imported": imported,
                }
            }), 200
    return jsonify({"suggestion": None}), 200


@bp.post("/import/chat-diagram")
@jwt_required()
@limiter.limit("10 per minute")
def import_chat_diagram(project_id: int):
    project = _project(project_id, "editor")
    payload = request.get_json(silent=True) or {}
    message_id = payload.get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 1:
        from ..errors import ValidationError

        raise ValidationError("'message_id' must be a positive integer.")
    result = chat_import.import_diagram(project, message_id)
    if result["created"]:
        record_event(
            action="topology.chat_diagram_import",
            result="success",
            user_id=current_user().id,
            details={
                "message_id": message_id,
                "devices": len(result["devices"]),
                "links": len(result["links"]),
            },
        )
    return jsonify(result), 201 if result["created"] else 200


@bp.post("/copy-chat-diagram")
@jwt_required()
@limiter.limit("5 per minute")
def copy_chat_diagram(project_id: int):
    """Create an editable personal project from a diagram the user can view."""
    source = _project(project_id, "viewer")
    payload = request.get_json(silent=True) or {}
    message_id = payload.get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 1:
        from ..errors import ValidationError

        raise ValidationError("'message_id' must be a positive integer.")

    message, diagram = chat_import.get_suggestion(source.id, message_id)
    user = current_user()
    base_name = f"{source.name} - {diagram.title}"[:80].strip()
    name = base_name
    suffix = 2
    while db.session.query(Project.id).filter_by(owner_id=user.id, name=name).first():
        tail = f" ({suffix})"
        name = f"{base_name[:80 - len(tail)].rstrip()}{tail}"
        suffix += 1

    new_project = project_service.create_project(user, {
        "name": name,
        "description": f"Bản chỉnh sửa từ sơ đồ Chat #{message.id} của project {source.name}.",
        "management_network": source.management_network,
        "environment": source.environment,
        "addressing_pool": source.addressing_pool,
    })
    try:
        result = chat_import.import_diagram(new_project, message.id, source_project_id=source.id)
    except Exception:
        db.session.rollback()
        db.session.delete(new_project)
        db.session.commit()
        raise

    record_event(
        action="topology.chat_diagram_copy",
        result="success",
        user_id=user.id,
        details={"source_project_id": source.id, "project_id": new_project.id, "message_id": message.id},
    )
    result["project"] = project_service.project_view(user, new_project)
    return jsonify(result), 201


@bp.get("")
@jwt_required()
def get_topology(project_id: int):
    project = _project(project_id, "viewer")
    return jsonify(service.get_topology(project)), 200


@bp.get("/templates")
@jwt_required()
def list_templates(project_id: int):
    _project(project_id, "viewer")
    return jsonify({"items": list(DEVICE_TEMPLATES)}), 200


@bp.get("/blueprints")
@jwt_required()
def list_blueprints(project_id: int):
    _project(project_id, "viewer")
    return jsonify({"items": blueprints.catalog()}), 200


@bp.post("/blueprints/<key>")
@jwt_required()
@limiter.limit("20 per minute")
def build_blueprint(project_id: int, key: str):
    project = _project(project_id, "editor")
    result = blueprints.build(project, key, request.get_json(silent=True) or {})
    record_event(
        action="topology.blueprint",
        result="success",
        user_id=current_user().id,
        details={"blueprint": key, "devices": len(result["devices"]), "links": len(result["links"])},
    )
    return jsonify(result), 201


@bp.get("/notes")
@jwt_required()
def list_notes(project_id: int):
    project = _project(project_id, "viewer")
    return jsonify({"items": [n.to_dict() for n in notes.list_notes(project.id)]}), 200


@bp.post("/notes")
@jwt_required()
def create_note(project_id: int):
    project = _project(project_id, "editor")
    note = notes.create_note(project, request.get_json(silent=True) or {})
    return jsonify(note.to_dict()), 201


@bp.put("/notes/<int:note_id>")
@jwt_required()
def update_note(project_id: int, note_id: int):
    project = _project(project_id, "editor")
    note = notes.update_note(project, note_id, request.get_json(silent=True) or {})
    return jsonify(note.to_dict()), 200


@bp.delete("/notes/<int:note_id>")
@jwt_required()
def delete_note(project_id: int, note_id: int):
    project = _project(project_id, "editor")
    notes.delete_note(project, note_id)
    return "", 204


@bp.get("/versions")
@jwt_required()
def list_versions(project_id: int):
    project = _project(project_id, "viewer")
    return jsonify({"items": [v.to_dict() for v in versions.list_versions(project.id)]}), 200


@bp.post("/versions")
@jwt_required()
@limiter.limit("20 per minute")
def create_version(project_id: int):
    project = _project(project_id, "editor")
    version = versions.create_version(project, current_user(), request.get_json(silent=True) or {})
    record_event(action="topology.version_create", result="success", user_id=current_user().id,
                 details={"version_id": version.id, "name": version.name})
    return jsonify(version.to_dict()), 201


@bp.get("/versions/<int:version_id>/diff")
@jwt_required()
def diff_version(project_id: int, version_id: int):
    project = _project(project_id, "viewer")
    return jsonify(versions.diff(project, versions.get_version(project.id, version_id))), 200


@bp.post("/versions/<int:version_id>/restore")
@jwt_required()
@limiter.limit("10 per minute")
def restore_version(project_id: int, version_id: int):
    project = _project(project_id, "editor")
    version = versions.get_version(project.id, version_id)
    result = versions.restore(project, current_user(), version, request.get_json(silent=True) or {})
    record_event(action="topology.version_restore", result="success", user_id=current_user().id,
                 details={"version_id": version.id, **{k: result[k] for k in ("created", "pruned")}})
    return jsonify(result), 200


@bp.delete("/versions/<int:version_id>")
@jwt_required()
def delete_version(project_id: int, version_id: int):
    project = _project(project_id, "editor")
    versions.delete_version(project, version_id)
    return "", 204


@bp.get("/lint")
@jwt_required()
def lint_design(project_id: int):
    project = _project(project_id, "viewer")
    return jsonify(lint.lint(project)), 200


@bp.get("/ipam")
@jwt_required()
def get_ipam(project_id: int):
    project = _project(project_id, "viewer")
    return jsonify(ipam.summary(project)), 200


@bp.post("/quick-device")
@jwt_required()
def quick_device(project_id: int):
    project = _project(project_id, "editor")
    device = service.quick_add_device(project, request.get_json(silent=True) or {})
    record_event(
        action="device.create",
        result="success",
        user_id=current_user().id,
        device_id=device.id,
        details=device.to_dict(),
    )
    return jsonify(device.to_dict()), 201


@bp.post("/duplicate")
@jwt_required()
def duplicate(project_id: int):
    project = _project(project_id, "editor")
    result = service.duplicate_devices(project, request.get_json(silent=True) or {})
    record_event(
        action="topology.duplicate",
        result="success",
        user_id=current_user().id,
        details={"devices": [d["hostname"] for d in result["devices"]]},
    )
    return jsonify(result), 201


@bp.get("/devices/<int:device_id>/ports")
@jwt_required()
def device_ports(project_id: int, device_id: int):
    from ..devices import service as device_service
    from .templates import port_inventory

    project = _project(project_id, "viewer")
    device = device_service.get_device(device_id, project.id)
    return jsonify({"ports": port_inventory(project.id, device)}), 200


@bp.put("/layout")
@jwt_required()
def save_layout(project_id: int):
    project = _project(project_id, "editor")
    service.save_layout(project, request.get_json(silent=True) or {})
    return jsonify(service.get_topology(project)), 200


@bp.post("/links")
@jwt_required()
def create_link(project_id: int):
    project = _project(project_id, "editor")
    link = service.create_link(project, request.get_json(silent=True) or {})
    record_event(
        action="topology.link_create",
        result="success",
        user_id=current_user().id,
        details=link.to_dict(),
    )
    return jsonify(link.to_dict()), 201


@bp.put("/links/<int:link_id>")
@jwt_required()
def update_link(project_id: int, link_id: int):
    project = _project(project_id, "editor")
    payload = request.get_json(silent=True) or {}
    link = service.update_link(project, link_id, payload)
    record_event(
        action="topology.link_update",
        result="success",
        user_id=current_user().id,
        details={"link_id": link.id, "changes": payload},
    )
    return jsonify(link.to_dict()), 200


@bp.delete("/links/<int:link_id>")
@jwt_required()
def delete_link(project_id: int, link_id: int):
    project = _project(project_id, "editor")
    service.delete_link(project, link_id)
    record_event(
        action="topology.link_delete",
        result="success",
        user_id=current_user().id,
        details={"link_id": link_id},
    )
    return "", 204


def _config_request():
    from pydantic import ValidationError as PydanticValidationError

    from ..errors import ValidationError

    try:
        return ConfigRequestSchema.model_validate(request.get_json(silent=True) or {})
    except PydanticValidationError as exc:
        raise ValidationError(
            "Config request failed validation.", {"_root": [str(exc)]}
        ) from exc


@bp.post("/import/pnetlab/preview")
@jwt_required()
@limiter.limit("10 per minute")
def pnetlab_preview(project_id: int):
    from ..integrations import saved_login

    project = _project(project_id, "editor")
    payload, remember = saved_login.fill_payload(project, request.get_json(silent=True) or {})
    result = pnetlab_import.preview(project, payload)
    # Only a login that just worked is remembered.
    if remember:
        saved_login.remember(project, payload, current_user().id)
        record_event(action="topology.pnetlab_login.save", result="success",
                     user_id=current_user().id, project_id=project.id,
                     details={"url": payload.get("url"), "username": payload.get("username")})
    return jsonify(result), 200


@bp.post("/import/pnetlab")
@jwt_required()
@limiter.limit("10 per minute")
def pnetlab_import_route(project_id: int):
    from ..integrations import saved_login

    project = _project(project_id, "editor")
    payload, _ = saved_login.fill_payload(project, request.get_json(silent=True) or {})
    result = pnetlab_import.import_lab(project, payload)
    created = [d["hostname"] for d in result["devices"] if d["status"] == "created"]
    record_event(
        action="topology.pnetlab_import",
        result="success",
        user_id=current_user().id,
        details={
            "lab": payload.get("lab"),
            "url": payload.get("url"),
            "devices": created,
            "links": sum(1 for l in result["links"] if l["status"] == "created"),
        },
    )
    return jsonify(result), 200


@bp.post("/bootstrap/pnetlab/preview")
@jwt_required()
@limiter.limit("10 per minute")
def bootstrap_preview(project_id: int):
    """The console commands that would give each device its management IP and SSH."""
    from ..integrations import saved_login

    project = _project(project_id, "editor")
    payload, _ = saved_login.fill_payload(project, request.get_json(silent=True) or {})
    return jsonify(bootstrap.preview(project, payload)), 200


@bp.post("/bootstrap/pnetlab")
@jwt_required()
@limiter.limit("10 per minute")
def bootstrap_run(project_id: int):
    """Start configuring management IP and SSH on PNETLab nodes through their console.

    This writes to devices without the change workflow (they cannot be reached
    over SSH yet), so only the project owner or an ADMIN may run it. A PA-VM
    takes minutes to boot and commit, so the work runs as a background job:
    the answer is the job, and the page polls ``/bootstrap/jobs/<id>``.
    """
    from flask import current_app

    from ..integrations import saved_login

    project = _project(project_id, "owner")
    payload, _ = saved_login.fill_payload(project, request.get_json(silent=True) or {})
    bootstrap.validate_request(payload)  # a bad request fails now, not in the job
    job = bootstrap_jobs.start(
        current_app._get_current_object(), project.id, current_user().id, payload, bootstrap.run
    )
    return jsonify({"job": job}), 202


@bp.get("/pnetlab-login")
@jwt_required()
def get_pnetlab_login(project_id: int):
    """The remembered PNETLab address and account (never the password)."""
    from ..integrations import saved_login

    project = _project(project_id, "editor")
    record = saved_login.get_saved(project.id)
    return jsonify({"saved": record.to_dict() if record else None}), 200


@bp.delete("/pnetlab-login")
@jwt_required()
def delete_pnetlab_login(project_id: int):
    from ..integrations import saved_login

    project = _project(project_id, "editor")
    saved_login.forget(project)
    record_event(action="topology.pnetlab_login.delete", result="success",
                 user_id=current_user().id, project_id=project.id)
    return jsonify({"saved": None}), 200


@bp.get("/bootstrap/jobs/<job_id>")
@jwt_required()
def bootstrap_job(project_id: int, job_id: str):
    project = _project(project_id, "owner")
    return jsonify({"job": bootstrap_jobs.get(job_id, project.id)}), 200


@bp.get("/design")
@jwt_required()
def get_design(project_id: int):
    project = _project(project_id, "viewer")
    return jsonify(design.list_design(project)), 200


@bp.post("/design/<kind>")
@jwt_required()
def add_design_item(project_id: int, kind: str):
    project = _project(project_id, "editor")
    item = design.create_item(project, kind, request.get_json(silent=True) or {})
    record_event(
        action="topology.design_add",
        result="success",
        user_id=current_user().id,
        details={"kind": kind, **item.to_dict()},
    )
    return jsonify(item.to_dict()), 201


@bp.delete("/design/<kind>/<int:item_id>")
@jwt_required()
def delete_design_item(project_id: int, kind: str, item_id: int):
    project = _project(project_id, "editor")
    design.delete_item(project, kind, item_id)
    record_event(
        action="topology.design_delete",
        result="success",
        user_id=current_user().id,
        details={"kind": kind, "item_id": item_id},
    )
    return "", 204


@bp.post("/discover")
@jwt_required()
@limiter.limit("6 per minute")
def discover_route(project_id: int):
    """Ask the devices who their neighbors are and compare with the design."""
    project = _project(project_id, "editor")
    result = discovery.discover(project)
    record_event(
        action="topology.discover",
        result="success",
        user_id=current_user().id,
        details={"polled": result["polled"], "errors": len(result["errors"]),
                 "unplanned": len(result["unplanned"]), "missing": len(result["missing"])},
    )
    return jsonify(result), 200


@bp.post("/discover/apply")
@jwt_required()
def discover_apply(project_id: int):
    project = _project(project_id, "editor")
    payload = request.get_json(silent=True) or {}
    links = payload.get("links")
    if not isinstance(links, list) or not all(isinstance(i, dict) for i in links):
        from ..errors import ValidationError
        raise ValidationError("'links' must be a list of discovered links.")
    result = discovery.apply_discovered(project, links)
    record_event(
        action="topology.discover_apply",
        result="success",
        user_id=current_user().id,
        details={"created": len(result["created"]), "skipped": len(result["skipped"])},
    )
    return jsonify(result), 200


@bp.post("/config-plan")
@jwt_required()
def config_plan(project_id: int):
    """Show the commands the design would produce. Changes nothing."""
    project = _project(project_id, "editor")
    data = _config_request()
    return jsonify(configgen.build_plan(project, data.link_ids)), 200


@bp.post("/config-preview")
@jwt_required()
def config_preview(project_id: int):
    """Freeze the design's configuration as a change batch awaiting approval.

    Needs editor access to the project and, like any change, a config.create
    permission (an ADMIN, or a scoped group grant). Approving and applying it
    stays an ADMIN action.
    """
    project = _project(project_id, "editor")
    data = _config_request()
    user = current_user()
    batch, plan = configgen.create_design_preview(
        project, user.id if user else None, data.link_ids, data.note
    )
    record_event(
        action="topology.config_preview",
        result="success",
        user_id=user.id if user else None,
        details={"batch_id": batch.id, "devices": [d["hostname"] for d in plan["devices"]]},
    )
    return jsonify({"batch": batch.to_dict(), "skipped": plan["skipped"]}), 201
