from flask import Blueprint, current_app, jsonify, request
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

    if not isinstance(username, str) or not isinstance(password, str) or not username or not password or len(password) > 128 or len(username) > 64:
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

    from ..governance import mfa
    if user.mfa_enabled:
        valid = mfa.verify(user, payload.get("code"))
        if not valid and payload.get("recovery_code"):
            valid = mfa.recover(user, payload["recovery_code"])
        if not valid:
            return jsonify(error="unauthorized", message="A valid two-factor or recovery code is required."), 401
    pending = user.role == "ADMIN" and current_app.config.get("ADMIN_MFA_REQUIRED", True) and not user.mfa_enabled
    record_event(
        action="auth.login",
        result="success",
        user_id=user.id,
        username=user.username,
        details={"role": user.role},
    )
    return jsonify({"access_token": issue_token(user, mfa_pending=pending), "user": user.to_dict(), "mfa_setup_required": pending}), 200


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
