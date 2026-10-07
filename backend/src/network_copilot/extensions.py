"""Flask extension singletons, instantiated once and bound by the app factory."""

from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData

# Predictable constraint names: migrations can drop or alter a constraint by
# name instead of guessing what the database generated. Explicitly named
# constraints keep their name; this only names the ones left anonymous.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

db = SQLAlchemy(metadata=MetaData(naming_convention=NAMING_CONVENTION))


def in_check(column: str, values, name: str):
    """CHECK (column IN (...)) so the database itself rejects a bad enum value."""
    quoted = ", ".join("'" + str(value).replace("'", "''") + "'" for value in values)
    return db.CheckConstraint(f"{column} IN ({quoted})", name=name)
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
