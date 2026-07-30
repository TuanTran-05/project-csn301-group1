from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from pydantic import ValidationError as PydanticValidationError

from ..auth.service import current_user
from ..chat.service import record_message as record_chat_message
from ..errors import AppError, ValidationError
from ..extensions import limiter
from .schemas import ChatRequest
from .service import AIService

bp = Blueprint("ai", __name__, url_prefix="/api/ai")


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
    try:
        result = AIService().handle(data.message, user_id)
    except AppError as exc:
        record_chat_message(
            user_id, username, "system", exc.message, {"error": exc.error}
        )
        raise
    record_chat_message(
        user_id, username, "assistant", result.get("explanation", ""), result
    )
    return jsonify(result), 200
