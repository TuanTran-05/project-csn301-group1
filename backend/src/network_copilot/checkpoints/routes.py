from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..auth.service import current_user
from ..extensions import limiter
from ..projects.scope import change_project, current_project, write_project
from . import service

bp = Blueprint("checkpoints", __name__, url_prefix="/api/checkpoints")


@bp.get("")
@jwt_required()
def list_checkpoints():
    project = current_project()
    return jsonify(service.list_checkpoints(
        project.id, limit=request.args.get("limit", default=50, type=int)
    )), 200


@bp.patch("/<int:checkpoint_id>")
@jwt_required()
def rename(checkpoint_id: int):
    project = write_project()
    step = service.get_checkpoint(checkpoint_id, project.id)
    user = current_user()
    payload = request.get_json(silent=True) or {}
    step = service.rename(step, payload.get("name"), user.id if user else None)
    return jsonify(step.to_dict()), 200


def _restore(checkpoint_id: int, action: str):
    project = change_project()
    step = service.get_checkpoint(checkpoint_id, project.id)
    user = current_user()
    change, skipped = service.request_restore(step, action, user.id if user else None)
    return jsonify({"change": change.to_dict(), "skipped": skipped,
                    "checkpoint": step.to_dict()}), 201


@bp.post("/<int:checkpoint_id>/undo")
@jwt_required()
@limiter.limit("10 per minute")
def undo(checkpoint_id: int):
    return _restore(checkpoint_id, "undo")


@bp.post("/<int:checkpoint_id>/redo")
@jwt_required()
@limiter.limit("10 per minute")
def redo(checkpoint_id: int):
    return _restore(checkpoint_id, "redo")
