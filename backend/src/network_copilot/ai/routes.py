from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from ..auth.service import current_user
from ..chat.service import record_message as record_chat_message
from ..errors import ValidationError
from ..extensions import limiter
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

    user = current_user()
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {"error": "request_failed", "message": "Request failed."}
    content = payload.get("message")
    if not isinstance(content, str):
        content = "Request failed."
    record_chat_message(
        user.id if user else None,
        user.username if user else None,
        "system",
        content,
        payload,
    )
    return response


@bp.post("/chat")
@jwt_required()
@limiter.limit("20 per minute")
def chat():
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

    record_chat_message(user_id, username, "user", data.message)
    result = AIService().handle(data.message, user_id)
    record_chat_message(
        user_id, username, "assistant", result.get("explanation", ""), result
    )
    return jsonify(result), 200
