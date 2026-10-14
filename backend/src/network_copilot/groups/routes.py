from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..auth.service import current_user
from ..errors import ValidationError
from . import service

bp = Blueprint("groups", __name__, url_prefix="/api/groups")


@bp.patch("/<int:group_id>/auto-approve")
@jwt_required()
def set_auto_approve(group_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "is_auto_approve" not in payload:
        raise ValidationError("is_auto_approve is required and must be a boolean.")
    user = current_user()
    group = service.set_auto_approve(
        group_id,
        user.id if user else None,
        payload["is_auto_approve"],
    )
    return jsonify(
        {"id": group.id, "name": group.name, "is_auto_approve": group.is_auto_approve}
    ), 200
