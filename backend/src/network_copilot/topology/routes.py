from flask import Blueprint, jsonify, request

from flask_jwt_extended import jwt_required

from ..audit.service import record_event
from ..auth.service import current_user, roles_required
from ..projects import service as project_service
from . import configgen, service
from .schemas import ConfigRequestSchema

# Topology routes name the project in the URL, unlike the inventory routes
# that use the X-Project-Id header: the diagram is addressed as a sub-resource
# of its project.
bp = Blueprint("topology", __name__, url_prefix="/api/projects/<int:project_id>/topology")


def _project(project_id: int, need: str):
    from flask import g

    project = project_service.require_project(current_user(), project_id, need)
    g.project_id = project.id
    return project


@bp.get("")
@jwt_required()
def get_topology(project_id: int):
    project = _project(project_id, "viewer")
    return jsonify(service.get_topology(project)), 200


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


@bp.post("/config-plan")
@jwt_required()
def config_plan(project_id: int):
    """Show the commands the design would produce. Changes nothing."""
    project = _project(project_id, "editor")
    data = _config_request()
    return jsonify(configgen.build_plan(project, data.link_ids)), 200


@bp.post("/config-preview")
@roles_required("ADMIN")
def config_preview(project_id: int):
    """Freeze the design's configuration as a change batch awaiting approval."""
    project = _project(project_id, "editor")
    data = _config_request()
    user = current_user()
    batch, plan = configgen.create_design_preview(
        project, user.id if user else None, data.link_ids
    )
    record_event(
        action="topology.config_preview",
        result="success",
        user_id=user.id if user else None,
        details={"batch_id": batch.id, "devices": [d["hostname"] for d in plan["devices"]]},
    )
    return jsonify({"batch": batch.to_dict(), "skipped": plan["skipped"]}), 201
