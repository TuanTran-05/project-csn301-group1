import hashlib
import logging
import os
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
    _register_static_versioning(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _start_scheduler(app)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/projects")
    def projects_page():
        return render_template("projects.html")

    @app.get("/users")
    def users_page():
        return render_template("users.html")

    @app.get("/dashboard")
    def dashboard_page():
        return render_template("dashboard.html")

    return app


def _register_extensions(app: Flask) -> None:
    db.init_app(app)
    _enforce_sqlite_foreign_keys(app)
    migrate.init_app(app, db)
    jwt.init_app(app)

    from .auth.service import is_token_revoked

    jwt.token_in_blocklist_loader(is_token_revoked)

    app.config.setdefault("RATELIMIT_HEADERS_ENABLED", True)
    app.config["RATELIMIT_ENABLED"] = bool(app.config.get("RATELIMIT_ENABLED", True))
    app.config["RATELIMIT_STORAGE_URI"] = app.config.get(
        "RATELIMIT_STORAGE_URI", "memory://"
    )
    limiter.init_app(app)


def _enforce_sqlite_foreign_keys(app: Flask) -> None:
    """Turn on FOREIGN KEY enforcement for every SQLite connection.

    Alembic's env.py switches it off again for the duration of a migration:
    SQLite rebuilds a table to alter it, and dropping the old table with
    enforcement on would cascade-delete its children.
    """
    if not app.config.get("SQLITE_FOREIGN_KEYS"):
        return

    from sqlalchemy import event

    with app.app_context():
        engine = db.engine
        if engine.dialect.name != "sqlite":
            return

        @event.listens_for(engine, "connect")
        def _pragma(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()


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
    from .groups import model as _group_model  # noqa: F401
    from .monitoring import model as _monitoring_model  # noqa: F401
    from .projects import model as _project_model  # noqa: F401
    from .topology import model as _topology_model  # noqa: F401


def _register_static_versioning(app: Flask) -> None:
    """Give every static URL a content hash (?v=...) so a deploy is never hidden
    by a browser's cached copy of an older script or stylesheet.

    Pages are sent with ``Cache-Control: no-cache`` for the same reason: a
    cached page would still point at the old URLs.
    """
    versions: dict[str, tuple[float, str]] = {}

    def version(filename: str) -> str | None:
        path = os.path.join(app.static_folder or "", filename)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cached = versions.get(filename)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()[:12]
        versions[filename] = (mtime, digest)
        return digest

    @app.url_defaults
    def add_static_version(endpoint, values):
        if endpoint == "static" and "filename" in values and "v" not in values:
            digest = version(values["filename"])
            if digest:
                values["v"] = digest

    @app.after_request
    def no_cache_pages(response):
        if response.mimetype == "text/html":
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def assign_request_id():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        from flask_jwt_extended import verify_jwt_in_request
        if request.headers.get("Authorization"):
            verify_jwt_in_request()

    @app.after_request
    def apply_security_headers(response):
        if response.is_json and request.path.startswith("/api/") and not request.path.startswith("/api/auth/"):
            from .governance.visibility import request_user
            from .governance.access import role
            user = request_user()
            if user and role(user) != "ADMIN":
                from .governance.redaction import redact_output
                response.set_data(app.json.dumps(redact_output(response.get_json())))
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
    from .auth.routes import users_bp
    from .changes.batch_routes import bp as change_batches_bp
    from .changes.routes import bp as changes_bp
    from .chat.routes import bp as chat_bp
    from .commands.routes import bp as commands_bp
    from .dashboard.routes import bp as dashboard_bp
    from .devices.routes import bp as devices_bp
    from .governance.learning import bp as learning_bp
    from .governance.routes import bp as governance_bp
    from .groups.routes import bp as groups_bp
    from .history_extension.routes import init_app as init_history_extension
    from .monitoring.routes import bp as monitoring_bp
    from .projects.routes import bp as projects_bp
    from .realtime.routes import bp as realtime_bp
    from .topology.routes import bp as topology_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(realtime_bp)
    app.register_blueprint(topology_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(monitoring_bp)
    app.register_blueprint(changes_bp)
    app.register_blueprint(change_batches_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(groups_bp)
    app.register_blueprint(governance_bp)
    app.register_blueprint(learning_bp)
    init_history_extension(app)


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
        db.session.rollback()
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

    @jwt.token_in_blocklist_loader
    def token_revoked(header, payload):
        from .auth.model import User
        try:
            user = db.session.get(User, int(payload["sub"]))
        except (ValueError, TypeError, KeyError):
            return True
        if not user or not user.is_active or payload.get("tv", 0) != user.token_version:
            return True
        return bool(user.role == "ADMIN" and app.config.get("ADMIN_MFA_REQUIRED", True)
                    and not user.mfa_enabled and not payload.get("mfa_pending"))

def _start_scheduler(app: Flask) -> None:
    from .monitoring.scheduler import init_scheduler

    init_scheduler(app)
