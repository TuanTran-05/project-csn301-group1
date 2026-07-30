from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from . import service

bp = Blueprint("chat", __name__, url_prefix="/api/chat")


@bp.get("/messages")
@jwt_required()
def list_messages():
    limit = request.args.get("limit", default=200, type=int)
    messages = service.list_messages(limit=limit)
    return jsonify({"items": [message.to_dict() for message in messages]}), 200
