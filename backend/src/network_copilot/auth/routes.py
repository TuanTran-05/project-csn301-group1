from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..audit.service import record_event
from ..extensions import limiter
from .service import (
    authenticate,
    create_user,
    current_user,
    issue_token,
    list_all_users,
    change_own_password,
    get_user,
    record_login,
    reset_password,
    roles_required,
    update_user,
)

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

    record_login(user)
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


@bp.post("/change-password")
@jwt_required()
def change_password():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    change_own_password(
        user, payload.get("current_password"), payload.get("new_password")
    )
    record_event(
        action="auth.password_change", result="success", user_id=user.id
    )
    # Every older token just stopped working, including the caller's own.
    return jsonify({"access_token": issue_token(user), "user": user.to_dict()}), 200


# -- user administration ----------------------------------------------------

users_bp = Blueprint("users", __name__, url_prefix="/api/users")


@users_bp.get("")
@roles_required("ADMIN")
def list_users():
    return jsonify({"items": [u.to_dict() for u in list_all_users()]}), 200


@users_bp.post("")
@roles_required("ADMIN")
def create_user_route():
    actor = current_user()
    user = create_user(request.get_json(silent=True) or {}, actor)
    record_event(
        action="user.create",
        result="success",
        user_id=actor.id,
        details={"username": user.username, "role": user.role},
    )
    return jsonify(user.to_dict()), 201


@users_bp.get("/<int:user_id>")
@roles_required("ADMIN")
def get_user_route(user_id: int):
    return jsonify(get_user(user_id).to_dict()), 200


@users_bp.put("/<int:user_id>")
@roles_required("ADMIN")
def update_user_route(user_id: int):
    actor = current_user()
    user = get_user(user_id)
    payload = request.get_json(silent=True) or {}
    update_user(user, payload, actor)
    record_event(
        action="user.update",
        result="success",
        user_id=actor.id,
        details={"target": user.username, "changes": payload},
    )
    return jsonify(user.to_dict()), 200


@users_bp.post("/<int:user_id>/reset-password")
@roles_required("ADMIN")
def reset_password_route(user_id: int):
    actor = current_user()
    user = get_user(user_id)
    payload = request.get_json(silent=True) or {}
    reset_password(user, payload.get("new_password"))
    record_event(
        action="user.password_reset",
        result="success",
        user_id=actor.id,
        details={"target": user.username},
    )
    return "", 204
