from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from pydantic import ValidationError as PydanticValidationError

from ..auth.service import current_user, roles_required
from ..errors import ValidationError
from ..extensions import limiter
from . import service
from .schemas import ChangePreviewSchema

bp = Blueprint("changes", __name__, url_prefix="/api/changes")


def _parse(schema, payload: dict):
    try:
        return schema.model_validate(payload)
    except PydanticValidationError as exc:
        details: dict[str, list[str]] = {}
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "_root"
            details.setdefault(field, []).append(error["msg"])
        raise ValidationError("Change payload failed validation.", details) from exc


@bp.post("/preview")
@roles_required("ADMIN")
def preview():
    data = _parse(ChangePreviewSchema, request.get_json(silent=True) or {})
    user = current_user()
    change = service.create_preview(
        user_id=user.id if user else None,
        device_id=data.device_id,
        commands=data.commands,
        verification_commands=data.verification_commands,
        description=data.description,
        execution_mode=data.execution_mode,
    )
    return jsonify(change.to_dict()), 201


@bp.get("")
@jwt_required()
def list_changes():
    device_id = request.args.get("device_id", type=int)
    changes = service.list_changes(
        device_id=device_id,
        status=request.args.get("status"),
        limit=request.args.get("limit", default=100, type=int),
    )
    return jsonify({"items": [change.to_dict() for change in changes]}), 200


@bp.get("/<int:change_id>")
@jwt_required()
def get_change(change_id: int):
    return jsonify(service.get_change(change_id).to_dict()), 200


@bp.post("/<int:change_id>/approve")
@roles_required("ADMIN")
def approve(change_id: int):
    user = current_user()
    change = service.approve(change_id, user.id if user else None)
    return jsonify(change.to_dict()), 200


@bp.post("/<int:change_id>/apply")
@roles_required("ADMIN")
@limiter.limit("10 per minute")
def apply(change_id: int):
    user = current_user()
    payload = request.get_json(silent=True) or {}
    change = service.apply(
        change_id, user.id if user else None, confirm_hostname=payload.get("confirm_hostname")
    )
    return jsonify(change.to_dict()), 200


@bp.post("/<int:change_id>/cancel")
@roles_required("ADMIN")
def cancel(change_id: int):
    user = current_user()
    change = service.cancel(change_id, user.id if user else None)
    return jsonify(change.to_dict()), 200
