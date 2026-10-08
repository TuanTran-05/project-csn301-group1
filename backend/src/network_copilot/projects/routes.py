import json

from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import jwt_required

from ..audit.service import record_event
from ..auth.service import current_user
from ..errors import ValidationError
from . import exchange, service

bp = Blueprint("projects", __name__, url_prefix="/api/projects")


@bp.get("")
@jwt_required()
def list_projects():
    user = current_user()
    items = service.project_views(user, service.accessible_projects(user))
    return jsonify({"items": items}), 200


@bp.post("")
@jwt_required()
def create_project():
    user = current_user()
    project = service.create_project(user, request.get_json(silent=True) or {})
    record_event(
        action="project.create",
        result="success",
        user_id=user.id,
        project_id=project.id,
        details=project.to_dict(),
    )
    return jsonify(service.project_view(user, project)), 201


@bp.get("/<int:project_id>")
@jwt_required()
def get_project(project_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "viewer")
    return jsonify(service.project_view(user, project)), 200


@bp.put("/<int:project_id>")
@jwt_required()
def update_project(project_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "owner")
    payload = request.get_json(silent=True) or {}
    service.update_project(project, payload)
    record_event(
        action="project.update",
        result="success",
        user_id=user.id,
        project_id=project.id,
        details={"changes": payload},
    )
    return jsonify(service.project_view(user, project)), 200


@bp.delete("/<int:project_id>")
@jwt_required()
def delete_project(project_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "owner")
    details = project.to_dict()
    service.delete_project(project)
    record_event(
        action="project.delete",
        result="success",
        user_id=user.id,
        details=details,
    )
    return "", 204


@bp.get("/<int:project_id>/members")
@jwt_required()
def list_members(project_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "viewer")
    return (
        jsonify({"items": [m.to_dict() for m in service.list_members(project)]}),
        200,
    )


@bp.post("/<int:project_id>/members")
@jwt_required()
def share_project(project_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "owner")
    payload = request.get_json(silent=True) or {}
    member = service.share_project(project, user, payload)
    record_event(
        action="project.share",
        result="success",
        user_id=user.id,
        project_id=project.id,
        details={"username": member.user.username, "access": member.access},
    )
    return jsonify(member.to_dict()), 201


@bp.put("/<int:project_id>/members/<int:user_id>")
@jwt_required()
def update_member(project_id: int, user_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "owner")
    payload = request.get_json(silent=True) or {}
    member = service.update_member(project, user_id, payload)
    record_event(
        action="project.share_update",
        result="success",
        user_id=user.id,
        project_id=project.id,
        details={"member_id": user_id, "access": member.access},
    )
    return jsonify(member.to_dict()), 200


@bp.delete("/<int:project_id>/members/<int:user_id>")
@jwt_required()
def remove_member(project_id: int, user_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "owner")
    service.remove_member(project, user_id)
    record_event(
        action="project.unshare",
        result="success",
        user_id=user.id,
        project_id=project.id,
        details={"member_id": user_id},
    )
    return "", 204


# -- import / export / clone ---------------------------------------------------


@bp.get("/<int:project_id>/export")
@jwt_required()
def export_project(project_id: int):
    """The project as a JSON document (no credentials)."""
    user = current_user()
    project = service.require_project(user, project_id, "viewer")
    document = exchange.export_project(project)
    record_event(action="project.export", result="success", user_id=user.id, project_id=project.id)
    filename = "".join(c if c.isalnum() or c in "-_" else "_" for c in project.name) or "project"
    return Response(
        json.dumps(document, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
    )


@bp.get("/<int:project_id>/devices.csv")
@jwt_required()
def export_devices_csv(project_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "viewer")
    return Response(
        exchange.devices_csv(project),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="devices.csv"'},
    )


@bp.post("/import")
@jwt_required()
def import_project():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    document = payload.get("document")
    if not isinstance(document, dict):
        raise ValidationError("'document' must be an exported project document.")
    project = exchange.import_project(user, document, payload.get("name") or None)
    record_event(
        action="project.import", result="success", user_id=user.id, project_id=project.id,
        details={"name": project.name},
    )
    return jsonify(service.project_view(user, project)), 201


@bp.post("/<int:project_id>/clone")
@jwt_required()
def clone_project(project_id: int):
    user = current_user()
    source = service.require_project(user, project_id, "viewer")
    payload = request.get_json(silent=True) or {}
    project = exchange.clone_project(user, source, payload.get("name") or None)
    record_event(
        action="project.clone", result="success", user_id=user.id, project_id=project.id,
        details={"source_id": source.id},
    )
    return jsonify(service.project_view(user, project)), 201


@bp.post("/<int:project_id>/devices/import-csv")
@jwt_required()
def import_devices_csv(project_id: int):
    user = current_user()
    project = service.require_project(user, project_id, "editor")
    payload = request.get_json(silent=True) or {}
    created = exchange.import_devices_csv(project, payload.get("csv"))
    record_event(
        action="device.csv_import", result="success", user_id=user.id, project_id=project.id,
        details={"count": len(created), "devices": [d.hostname for d in created]},
    )
    return jsonify({"created": [d.to_dict() for d in created]}), 201
