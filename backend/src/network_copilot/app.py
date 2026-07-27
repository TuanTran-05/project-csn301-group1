from flask import Flask, jsonify

from .config import Config
from .errors import AppError
from .extensions import db, jwt, migrate


def create_app(config_object: type | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object or Config)

    _register_extensions(app)
    _register_models()
    _register_blueprints(app)
    _register_error_handlers(app)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    return app


def _register_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)


def _register_models() -> None:
    """Import models so SQLAlchemy and Alembic see every table."""
    from .auth import model as _auth_model  # noqa: F401
    from .devices import model as _device_model  # noqa: F401


def _register_blueprints(app: Flask) -> None:
    from .auth.routes import bp as auth_bp
    from .devices.routes import bp as devices_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(devices_bp)


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError):
        return jsonify(exc.to_dict()), exc.status_code
