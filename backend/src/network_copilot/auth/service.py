import re
from functools import wraps

from flask import jsonify
from flask_jwt_extended import create_access_token, get_jwt, get_jwt_identity, verify_jwt_in_request

from ..errors import ConflictError, ValidationError
from ..extensions import db
from .model import ROLES, User

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
MIN_PASSWORD_LENGTH = 10


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


def list_all_users() -> list[User]:
    return db.session.query(User).order_by(User.username).all()


def create_user(payload: dict) -> User:
    """Create an account. Admin-only at the route; validates every field."""
    username = str(payload.get("username") or "").strip()
    password = payload.get("password")
    role = payload.get("role", "OPERATOR")

    details: dict[str, list[str]] = {}
    if not USERNAME_PATTERN.match(username):
        details["username"] = [
            "3-64 characters: letters, digits, '.', '_' or '-'."
        ]
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        details["password"] = [
            f"Must be at least {MIN_PASSWORD_LENGTH} characters."
        ]
    if role not in ROLES:
        details["role"] = [f"Must be one of {', '.join(ROLES)}."]
    if details:
        raise ValidationError("User payload failed validation.", details)

    if db.session.query(User).filter_by(username=username).first() is not None:
        raise ConflictError(f"A user named {username} already exists.")

    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user
