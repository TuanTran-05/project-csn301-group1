from functools import wraps

from flask import jsonify
from flask_jwt_extended import create_access_token, get_jwt, get_jwt_identity, verify_jwt_in_request

from ..extensions import db
from .model import User


def authenticate(username: str, password: str) -> User | None:
    """Return the user when the credentials match, otherwise None."""
    user = db.session.query(User).filter_by(username=username).one_or_none()
    if user is None or not user.is_active:
        return None
    if not user.check_password(password):
        return None
    return user


def issue_token(user: User) -> str:
    return create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "username": user.username},
    )


def current_user() -> User | None:
    identity = get_jwt_identity()
    if identity is None:
        return None
    return db.session.get(User, int(identity))


def roles_required(*roles: str):
    """Reject requests whose JWT role claim is not in ``roles``."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            verify_jwt_in_request()
            claims = get_jwt()
            if claims.get("role") not in roles:
                return (
                    jsonify(
                        {
                            "error": "forbidden",
                            "message": "Role is not permitted to perform this action.",
                            "required_roles": list(roles),
                        }
                    ),
                    403,
                )
            return view(*args, **kwargs)

        return wrapper

    return decorator
