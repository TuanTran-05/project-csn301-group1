import logging
import uuid

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .config import Config
from .errors import AppError, error_payload
from .extensions import db, jwt, limiter, migrate

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def create_app(config_object: type | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object or Config)

    _register_extensions(app)
    _register_models()
    _register_request_hooks(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _start_scheduler(app)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/dashboard")
    def dashboard_page():
        return render_template("dashboard.html")

    return app


def _register_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)

    app.config.setdefault("RATELIMIT_HEADERS_ENABLED", True)
    app.config["RATELIMIT_ENABLED"] = bool(app.config.get("RATELIMIT_ENABLED", True))
    app.config["RATELIMIT_STORAGE_URI"] = app.config.get(
        "RATELIMIT_STORAGE_URI", "memory://"
    )
    limiter.init_app(app)


def _register_models() -> None:
    """Import models so SQLAlchemy and Alembic see every table."""
    from .audit import model as _audit_model  # noqa: F401
    from .auth import model as _auth_model  # noqa: F401
    from .backups import model as _backup_model  # noqa: F401
    from .changes import model as _change_model  # noqa: F401
    from .chat import model as _chat_model  # noqa: F401
    from .commands import model as _command_model  # noqa: F401
    from .credentials import model as _credential_model  # noqa: F401
    from .devices import model as _device_model  # noqa: F401
    from .monitoring import model as _monitoring_model  # noqa: F401


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def assign_request_id():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    @app.after_request
    def apply_security_headers(response):
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        request_id = getattr(g, "request_id", None)
        if request_id:
            response.headers["X-Request-ID"] = request_id
        return response


def _register_blueprints(app: Flask) -> None:
    from .ai.routes import bp as ai_bp
    from .audit.routes import bp as audit_bp
    from .auth.routes import bp as auth_bp
    from .changes.batch_routes import bp as change_batches_bp
    from .changes.routes import bp as changes_bp
    from .chat.routes import bp as chat_bp
    from .commands.routes import bp as commands_bp
    from .dashboard.routes import bp as dashboard_bp
    from .devices.routes import bp as devices_bp
    from .monitoring.routes import bp as monitoring_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(monitoring_bp)
    app.register_blueprint(changes_bp)
    app.register_blueprint(change_batches_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(dashboard_bp)


def _register_error_handlers(app: Flask) -> None:
    def _respond(status_code: int, message: str, details: dict | None = None):
        payload = error_payload(status_code, message, details)
        payload["request_id"] = getattr(g, "request_id", None)
        return jsonify(payload), status_code

    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError):
        # Keep the exception's own code: several failures share one HTTP status
        # (policy_violation and forbidden are both 403, ssh_timeout and
        # device_unreachable are both 502) and clients need to tell them apart.
        payload = exc.to_dict()
        payload["request_id"] = getattr(g, "request_id", None)
        return jsonify(payload), exc.status_code

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        # Werkzeug's default description is safe to surface; it never contains
        # application state.
        return _respond(exc.code or 500, exc.description or "Request failed.")

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        # The full exception goes to the server log only. The client gets a
        # generic message so nothing internal can leak.
        logger.exception(
            "Unhandled error on %s (request_id=%s)",
            request.path if request else "?",
            getattr(g, "request_id", None),
        )
        db.session.rollback()
        return _respond(500, "An internal error occurred.")

    @app.errorhandler(429)
    def handle_rate_limit(exc):
        return _respond(
            429, "Too many requests. Slow down and try again in a moment."
        )

    # Flask-JWT-Extended answers with its own {"msg": ...} shape by default.
    # Route every auth failure through the same contract as the rest of the API.
    @jwt.unauthorized_loader
    def handle_missing_token(reason: str):
        return _respond(401, "Authentication is required.")

    @jwt.invalid_token_loader
    def handle_invalid_token(reason: str):
        return _respond(401, "The access token is invalid.")

    @jwt.expired_token_loader
    def handle_expired_token(header, payload):
        return _respond(401, "The access token has expired.")

    @jwt.revoked_token_loader
    def handle_revoked_token(header, payload):
        return _respond(401, "The access token has been revoked.")


def _start_scheduler(app: Flask) -> None:
    from .monitoring.scheduler import init_scheduler

    init_scheduler(app)
