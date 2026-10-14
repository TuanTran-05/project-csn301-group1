from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..auth.service import current_user
from ..projects.scope import current_project
from . import service, session_service

bp = Blueprint("chat", __name__, url_prefix="/api/chat")


@bp.get("/messages")
@jwt_required()
def list_messages():
    limit = request.args.get("limit", default=200, type=int)
    session_id = request.args.get("session_id", type=int)
    project = current_project()
    messages = service.list_messages(
        session_id=session_id, limit=limit, project_id=project.id
    )
    return jsonify({"items": [message.to_dict() for message in messages]}), 200


@bp.get("/sessions")
@jwt_required()
def list_sessions():
    project = current_project()
    return jsonify({"items": session_service.list_sessions(project.id)}), 200


@bp.post("/sessions")
@jwt_required()
def create_session():
    project = current_project()
    user = current_user()
    session = session_service.create_session(
        project.id, created_by_id=user.id if user else None
    )
    return jsonify(session_service.session_to_dict(session)), 201
