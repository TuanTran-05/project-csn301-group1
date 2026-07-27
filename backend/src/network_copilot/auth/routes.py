from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..audit.service import record_event
from ..extensions import limiter
from .service import authenticate, current_user, issue_token

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.post("/login")
@limiter.limit("5 per minute", key_func=lambda: request.remote_addr or "anonymous")
def login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username")
    password = payload.get("password")

    if not username or not password:
        return (
            jsonify(
                {
                    "error": "bad_request",
                    "message": "username and password are required.",
                }
            ),
            400,
        )

    user = authenticate(username, password)
    if user is None:
        # The submitted password is deliberately never passed to the audit log.
        record_event(
            action="auth.login",
            result="failure",
            username=username,
            message="Invalid credentials.",
        )
        return (
            jsonify({"error": "unauthorized", "message": "Invalid credentials."}),
            401,
        )

    record_event(
        action="auth.login",
        result="success",
        user_id=user.id,
        username=user.username,
        details={"role": user.role},
    )
    return jsonify({"access_token": issue_token(user), "user": user.to_dict()}), 200


@bp.get("/me")
@jwt_required()
def me():
    user = current_user()
    if user is None:
        return (
            jsonify({"error": "unauthorized", "message": "Unknown user."}),
            401,
        )
    return jsonify(user.to_dict()), 200
