from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..auth.service import current_user
from ..errors import ValidationError
from . import service

bp = Blueprint("commands", __name__, url_prefix="/api/commands")


def _int_arg(name: str) -> int | None:
    raw = request.args.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationError(f"'{name}' must be an integer.") from exc


@bp.post("/execute-readonly")
@jwt_required()
def execute_readonly():
    payload = request.get_json(silent=True) or {}
    device_id = payload.get("device_id")
    command = payload.get("command")

    if not isinstance(device_id, int):
        raise ValidationError("'device_id' is required and must be an integer.")

    user = current_user()
    execution = service.execute_readonly(
        device_id=device_id,
        command=command,
        user_id=user.id if user else None,
    )
    return jsonify(execution.to_dict()), 200


@bp.get("/history")
@jwt_required()
def history():
    executions = service.list_history(
        device_id=_int_arg("device_id"),
        user_id=_int_arg("user_id"),
        status=request.args.get("status"),
        limit=_int_arg("limit") or 100,
    )
    return jsonify({"items": [item.to_dict() for item in executions]}), 200
