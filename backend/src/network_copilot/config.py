import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SQLITE_PREFIX = "sqlite:///"


def _database_uri() -> str:
    """Resolve DATABASE_URL, anchoring relative sqlite paths to BASE_DIR.

    Flask resolves a relative sqlite path against its instance folder
    (``src/instance/``), so the same DATABASE_URL would point at a different
    file depending on how the process was started. Anchoring it here keeps
    ``flask db upgrade``, the seed scripts and the running server on one file.
    """
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        return f"{SQLITE_PREFIX}{BASE_DIR / 'network_copilot.db'}"

    if raw.startswith(SQLITE_PREFIX):
        path = raw[len(SQLITE_PREFIX) :]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"{SQLITE_PREFIX}{BASE_DIR / path}"
    return raw


class Config:
    """Base configuration, driven entirely by environment variables."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", SECRET_KEY)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(
        minutes=int(os.environ.get("JWT_ACCESS_TOKEN_MINUTES", "60"))
    )

    # Fernet key used to encrypt device credentials at rest. Never hardcode it
    # for anything other than tests.
    CREDENTIAL_ENCRYPTION_KEY = os.environ.get("CREDENTIAL_ENCRYPTION_KEY")

    # Management network every device must live in.
    MANAGEMENT_NETWORK = os.environ.get("MANAGEMENT_NETWORK", "10.10.10.0/24")

    SSH_CONNECT_TIMEOUT = int(os.environ.get("SSH_CONNECT_TIMEOUT", "10"))
    SSH_COMMAND_TIMEOUT = int(os.environ.get("SSH_COMMAND_TIMEOUT", "30"))

    MONITORING_ENABLED = _bool("MONITORING_ENABLED", False)
    MONITORING_INTERVAL_SECONDS = int(
        os.environ.get("MONITORING_INTERVAL_SECONDS", "60")
    )

    RATELIMIT_ENABLED = _bool("RATELIMIT_ENABLED", True)
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")

    AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic")
    AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5")
    AI_API_KEY = os.environ.get("AI_API_KEY")

    TESTING = False


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SECRET_KEY = "test-secret-key-for-pytest-only-0123456789"
    JWT_SECRET_KEY = "test-jwt-secret-key-for-pytest-only-0123456789"
    # Deterministic Fernet key so tests never depend on the host environment.
    CREDENTIAL_ENCRYPTION_KEY = "MsXBQh03EB9ifk_rNUsDK_F2FVJCYCz6BtuVTEYt9Hg="
    MONITORING_ENABLED = False
    RATELIMIT_ENABLED = False
