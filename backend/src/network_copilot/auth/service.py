import re
from functools import wraps

from flask import jsonify
from flask_jwt_extended import create_access_token, get_jwt, get_jwt_identity, verify_jwt_in_request

from ..errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ..extensions import db
from .model import ROLES, User

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
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
        additional_claims={
            "role": user.role,
            "username": user.username,
            "tv": user.token_version,
        },
    )


def is_token_revoked(_header, payload) -> bool:
    """A token dies with its account state.

    Deleting or deactivating the user, or changing their password or role
    (each bumps ``token_version``), invalidates every token issued before.
    """
    try:
        user = db.session.get(User, int(payload["sub"]))
    except (KeyError, TypeError, ValueError):
        return True
    if user is None or not user.is_active:
        return True
    return payload.get("tv", 0) != user.token_version


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
            # The role comes from the database, not from the token, so a
            # demotion takes effect immediately.
            user = current_user()
            role = user.role if user is not None else get_jwt().get("role")
            if role not in roles:
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


# -- user administration ----------------------------------------------------

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_PASSWORD_LENGTH = 128


def list_all_users() -> list[User]:
    return db.session.query(User).order_by(User.username).all()


def get_user(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")
    return user


def _check_password(password, details: dict, field: str = "password") -> None:
    if not isinstance(password, str) or not (
        MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH
    ):
        details[field] = [
            f"Must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters."
        ]


def _clean_optional(value, field: str, max_length: int, details: dict):
    if value is None:
        return None
    if not isinstance(value, str) or len(value.strip()) > max_length:
        details[field] = [f"Must be text of at most {max_length} characters."]
        return None
    return value.strip() or None


def _clean_email(value, details: dict):
    email = _clean_optional(value, "email", 254, details)
    if email is not None and not EMAIL_PATTERN.match(email):
        details["email"] = ["Must be a valid email address."]
        return None
    return email.lower() if email else None


def _assert_email_free(email: str | None, exclude_id: int | None = None) -> None:
    if email is None:
        return
    query = db.session.query(User).filter(db.func.lower(User.email) == email)
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    if query.first() is not None:
        raise ConflictError(f"The email {email} is already used by another account.")


def create_user(payload: dict, actor: User | None = None) -> User:
    """Create an account. Admin-only at the route; validates every field."""
    username = str(payload.get("username") or "").strip()
    password = payload.get("password")
    role = payload.get("role", "OPERATOR")

    details: dict[str, list[str]] = {}
    if not USERNAME_PATTERN.match(username):
        details["username"] = [
            "2-64 characters: letters, digits, '.', '_' or '-'."
        ]
    _check_password(password, details)
    if role not in ROLES:
        details["role"] = [f"Must be one of {', '.join(ROLES)}."]
    full_name = _clean_optional(payload.get("full_name"), "full_name", 120, details)
    email = _clean_email(payload.get("email"), details)
    if details:
        raise ValidationError("User payload failed validation.", details)

    if db.session.query(User).filter_by(username=username).first() is not None:
        raise ConflictError(f"A user named {username} already exists.")
    _assert_email_free(email)

    user = User(
        username=username,
        role=role,
        full_name=full_name,
        email=email,
        created_by_id=actor.id if actor else None,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _other_active_admins(user: User) -> int:
    return (
        db.session.query(db.func.count(User.id))
        .filter(User.role == "ADMIN", User.is_active.is_(True), User.id != user.id)
        .scalar()
    )


def update_user(user: User, payload: dict, actor: User) -> User:
    """Change profile, role or active flag.

    Refuses anything that would leave the system without an active ADMIN, or
    that lets an administrator lock themselves out by accident.
    """
    allowed = {"full_name", "email", "role", "is_active"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValidationError(
            "Unknown user fields.", {field: ["Not editable."] for field in sorted(unknown)}
        )

    details: dict[str, list[str]] = {}
    changes: dict = {}
    if "full_name" in payload:
        changes["full_name"] = _clean_optional(payload["full_name"], "full_name", 120, details)
    if "email" in payload:
        changes["email"] = _clean_email(payload["email"], details)
    if "role" in payload:
        if payload["role"] not in ROLES:
            details["role"] = [f"Must be one of {', '.join(ROLES)}."]
        else:
            changes["role"] = payload["role"]
    if "is_active" in payload:
        if not isinstance(payload["is_active"], bool):
            details["is_active"] = ["Must be true or false."]
        else:
            changes["is_active"] = payload["is_active"]
    if details:
        raise ValidationError("User payload failed validation.", details)

    new_role = changes.get("role", user.role)
    new_active = changes.get("is_active", user.is_active)
    losing_admin = user.role == "ADMIN" and user.is_active and not (
        new_role == "ADMIN" and new_active
    )
    if losing_admin:
        if user.id == actor.id:
            raise ConflictError(
                "You cannot remove your own administrator access or deactivate "
                "yourself; ask another administrator."
            )
        if _other_active_admins(user) == 0:
            raise ConflictError("At least one active administrator must remain.")
    if user.id == actor.id and new_active is False:
        raise ConflictError("You cannot deactivate your own account.")

    if "email" in changes:
        _assert_email_free(changes["email"], exclude_id=user.id)

    # Anything that changes what the account may do logs it out everywhere.
    if new_role != user.role or new_active != user.is_active:
        user.token_version += 1
    for field, value in changes.items():
        setattr(user, field, value)
    db.session.commit()
    return user


def reset_password(user: User, new_password) -> User:
    """Administrator sets a new password; the user's tokens stop working."""
    details: dict[str, list[str]] = {}
    _check_password(new_password, details, "new_password")
    if details:
        raise ValidationError("Password failed validation.", details)
    user.set_password(new_password)
    user.token_version += 1
    db.session.commit()
    return user


def change_own_password(user: User, current_password, new_password) -> User:
    if not isinstance(current_password, str) or not user.check_password(current_password):
        raise ForbiddenError("The current password is incorrect.")
    details: dict[str, list[str]] = {}
    _check_password(new_password, details, "new_password")
    if not details and new_password == current_password:
        details["new_password"] = ["Must differ from the current password."]
    if details:
        raise ValidationError("Password failed validation.", details)
    user.set_password(new_password)
    user.token_version += 1
    db.session.commit()
    return user


def record_login(user: User) -> None:
    from datetime import datetime, timezone

    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()
