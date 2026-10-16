from flask import Blueprint, current_app, g, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from ..auth.model import User
from ..auth.service import current_user
from ..chat.service import record_message as record_chat_message
from ..chat.session_service import create_session
from ..chat.onboarding import requests_project, requested_device_count, topology_options
from ..errors import ValidationError
from ..extensions import db, limiter
from ..projects.scope import current_project
from ..projects import service as project_service
from ..projects.model import Project
from ..audit.service import record_event
from .schemas import ChatRequest
from .service import AIService

bp = Blueprint("ai", __name__, url_prefix="/api/ai")


@bp.after_app_request
def record_failed_chat_response(response):
    """Store one safe transcript entry for each authenticated chat failure."""
    if request.endpoint != "ai.chat" or 200 <= response.status_code < 300:
        return response

    try:
        identity = get_jwt_identity()
    except RuntimeError:
        # JWT verification did not complete, so this is an unauthenticated 401.
        return response
    if identity is None:
        return response

    # No project was resolved (none chosen, or no access): there is no
    # transcript to put the failure in.
    project_id = getattr(g, "project_id", None)
    if project_id is None:
        return response

    user = current_user()
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {"error": "request_failed", "message": "Request failed."}
    content = payload.get("message")
    if not isinstance(content, str):
        content = "Request failed."
    body = request.get_json(silent=True) or {}
    raw_session_id = body.get("session_id") if isinstance(body, dict) else None
    session_id = raw_session_id if isinstance(raw_session_id, int) else None
    record_chat_message(
        user.id if user else None,
        user.username if user else None,
        "system",
        content,
        payload,
        session_id=session_id,
        project_id=project_id,
    )
    return response


def _new_project_name(user: User) -> str:
    prefix = f"{user.username} Lab"
    names = {
        project.name
        for project in db.session.query(Project).filter(Project.owner_id == user.id)
    }
    number = 1
    while f"{prefix} {number}" in names:
        number += 1
    return f"{prefix} {number}"


def _chat_reply(user, session_id, project_id, content, payload):
    record_chat_message(
        user.id, user.username, "assistant", content, payload,
        session_id=session_id, project_id=project_id,
    )
    return jsonify({"intent": payload.get("flow", "advice"), "explanation": content,
                    "session_id": session_id, **payload}), 200


@bp.post("/chat")
@jwt_required()
@limiter.limit("20 per minute")
def chat():
    project = current_project()
    try:
        data = ChatRequest.model_validate(request.get_json(silent=True) or {})
    except PydanticValidationError as exc:
        details: dict[str, list[str]] = {}
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "_root"
            details.setdefault(field, []).append(error["msg"])
        raise ValidationError("A non-empty 'message' is required.", details) from exc

    user = current_user()
    user_id = user.id if user else None
    username = user.username if user else None

    if user.role == "OPERATOR" and requests_project(data.message):
        created = project_service.create_project(user, {
            "name": _new_project_name(user),
            "description": "Project được khởi tạo từ khung chat.",
            "management_network": current_app.config["MANAGEMENT_NETWORK"],
            "environment": "pnetlab",
        })
        session = create_session(created.id, created_by_id=user.id)
        record_chat_message(user_id, username, "user", data.message,
                            session_id=session.id, project_id=created.id)
        record_event(action="project.create", result="success", user_id=user.id,
                     project_id=created.id, details={"source": "chat"})
        content = "Bạn muốn project có bao nhiêu thiết bị mạng? Hãy nhập một số từ 2 đến 25."
        payload = {"flow": "topology_setup_count", "project_id": created.id,
                   "project": project_service.project_view(user, created)}
        record_chat_message(user_id, username, "assistant", content, payload,
                            session_id=session.id, project_id=created.id)
        return jsonify({"intent": "project_created", "explanation": content,
                        "session_id": session.id, **payload}), 201

    if user.role == "OPERATOR" and data.session_id is not None:
        from ..chat.model import ChatMessage, ChatSession
        prompt = db.session.query(ChatMessage).filter_by(
            session_id=data.session_id, role="assistant"
        ).join(ChatSession).filter(
            ChatSession.project_id == project.id
        ).order_by(ChatMessage.id.desc()).first()
        if prompt and (prompt.payload or {}).get("flow") == "topology_setup_count":
            count = requested_device_count(data.message)
            record_chat_message(user_id, username, "user", data.message,
                                session_id=data.session_id, project_id=project.id)
            if count is None:
                content = "Nhập số thiết bị mạng từ 2 đến 25 để mình tạo các phương án topology."
                return _chat_reply(user, data.session_id, project.id, content,
                                   {"flow": "topology_setup_count", "project_id": project.id})
            options = topology_options(count)
            content = f"Mình đã chuẩn bị {len(options)} phương án cho {count} thiết bị mạng. Chọn một sơ đồ để tạo rồi chỉnh sửa trên canvas."
            return _chat_reply(user, data.session_id, project.id, content,
                               {"flow": "topology_options", "project_id": project.id,
                                "device_count": count, "options": options})

    record_chat_message(
        user_id, username, "user", data.message,
        session_id=data.session_id, project_id=project.id,
    )
    if user.role in {"OPERATOR", "VIEWER"}:
        result = AIService().advise(data.message, data.session_id, project.id)
    else:
        result = AIService().handle(
            data.message, user_id, session_id=data.session_id, project_id=project.id
        )
    record_chat_message(
        user_id, username, "assistant", result.get("explanation", ""), result,
        session_id=data.session_id, project_id=project.id,
    )
    return jsonify(result), 200
