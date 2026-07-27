"""Flask extension singletons, instantiated once and bound by the app factory."""

from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()


def rate_limit_key() -> str:
    """Limit per authenticated user when possible, otherwise per source IP."""
    try:
        from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request

        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
        if identity:
            return f"user:{identity}"
    except Exception:
        pass
    return f"ip:{get_remote_address()}"


limiter = Limiter(key_func=rate_limit_key)
