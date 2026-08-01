from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..auth.service import current_user, roles_required
from ..errors import ValidationError
from ..extensions import limiter
from . import batch_service

bp = Blueprint("change_batches", __name__, url_prefix="/api/change-batches")


@bp.get("")
@jwt_required()
def list_batches():
    batches = batch_service.list_batches(
        limit=request.args.get("limit", default=100, type=int)
    )
    return jsonify({"items": [batch.to_dict() for batch in batches]}), 200


@bp.get("/<int:batch_id>")
@jwt_required()
def get_batch(batch_id: int):
    return jsonify(batch_service.get_batch(batch_id).to_dict()), 200


@bp.post("/<int:batch_id>/approve")
@roles_required("ADMIN")
def approve(batch_id: int):
    user = current_user()
    batch = batch_service.approve_batch(batch_id, user.id if user else None)
    return jsonify(batch.to_dict()), 200


@bp.post("/<int:batch_id>/apply")
@roles_required("ADMIN")
@limiter.limit("10 per minute")
def apply(batch_id: int):
    user = current_user()
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValidationError("Batch apply payload must be a JSON object.")
    batch = batch_service.apply_batch(
        batch_id,
        user.id if user else None,
        confirmation=payload.get("confirmation"),
    )
    return jsonify(batch.to_dict()), 200


@bp.post("/<int:batch_id>/cancel")
@roles_required("ADMIN")
def cancel(batch_id: int):
    user = current_user()
    batch = batch_service.cancel_batch(batch_id, user.id if user else None)
    return jsonify(batch.to_dict()), 200
