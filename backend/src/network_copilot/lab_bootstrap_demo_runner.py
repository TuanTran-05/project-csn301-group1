"""Run only the inspection-only Safe Demo routes on loopback.

Invoke with ``python -m network_copilot.lab_bootstrap_demo_runner`` from the
backend environment. This runner intentionally does not use the full app
factory or register the application's other blueprints.
"""

import secrets

from flask import Flask, jsonify, request
from flask_jwt_extended import JWTManager

from .config import TestConfig
from .extensions import limiter
from .lab_bootstrap_demo import (
    SAFE_DEMO_TOKEN_MAX_AGE_SECONDS,
    SAFE_DEMO_TOKEN_TTL,
    bp as lab_bootstrap_demo_bp,
)

SAFE_DEMO_HOST = "127.0.0.1"
SAFE_DEMO_PORT = 5058
SAFE_DEMO_AUTHORITY = f"{SAFE_DEMO_HOST}:{SAFE_DEMO_PORT}"
SAFE_DEMO_ORIGIN = f"http://{SAFE_DEMO_AUTHORITY}"
SAFE_DEMO_SCRIPT_PATH = "/static/js/lab_bootstrap_demo.js"
SAFE_DEMO_ANALYZE_PATH = "/api/lab-bootstrap-demo/analyze-archive"
SAFE_DEMO_EXPORT_PATH = "/api/lab-bootstrap-demo/export-inspection"
SAFE_DEMO_POST_PATHS = frozenset({SAFE_DEMO_ANALYZE_PATH, SAFE_DEMO_EXPORT_PATH})
SAFE_DEMO_ACCESS_COOKIE_NAME = "lab_bootstrap_demo_access"
SAFE_DEMO_CSRF_COOKIE_NAME = "lab_bootstrap_demo_csrf"
SAFE_DEMO_CSRF_HEADER_NAME = "X-CSRF-TOKEN"


class SafeDemoConfig(TestConfig):
    """Keep demo data in RAM and scope short-lived JWTs to this runner."""

    SECRET_KEY = None
    JWT_SECRET_KEY = None
    JWT_ACCESS_TOKEN_EXPIRES = SAFE_DEMO_TOKEN_TTL
    JWT_TOKEN_LOCATION = ("cookies",)
    JWT_COOKIE_CSRF_PROTECT = True
    JWT_CSRF_IN_COOKIES = True
    JWT_COOKIE_SECURE = False  # The runner is HTTP on loopback only.
    JWT_COOKIE_SAMESITE = "Strict"
    JWT_ACCESS_COOKIE_NAME = SAFE_DEMO_ACCESS_COOKIE_NAME
    JWT_ACCESS_COOKIE_PATH = "/api/lab-bootstrap-demo/"
    JWT_ACCESS_CSRF_COOKIE_NAME = SAFE_DEMO_CSRF_COOKIE_NAME
    JWT_ACCESS_CSRF_COOKIE_PATH = "/lab-bootstrap-demo"
    JWT_ACCESS_CSRF_HEADER_NAME = SAFE_DEMO_CSRF_HEADER_NAME
    JWT_ENCODE_AUDIENCE = "lab-bootstrap-demo"
    JWT_DECODE_AUDIENCE = "lab-bootstrap-demo"
    JWT_ENCODE_ISSUER = "lab-bootstrap-safe-demo"
    JWT_DECODE_ISSUER = "lab-bootstrap-safe-demo"
    LAB_BOOTSTRAP_DEMO_COOKIE_AUTH = True
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"


def create_safe_demo_app() -> Flask:
    """Create a narrow app containing only the Safe Demo page and API."""
    app = Flask(
        __package__,
        static_folder=None,
        template_folder="templates",
    )
    app.config.from_object(SafeDemoConfig)
    # Never inherit TestConfig's deterministic key. A fresh key per app process
    # invalidates every previously issued demo token after restart.
    app.config["SECRET_KEY"] = secrets.token_urlsafe(32)
    app.config["JWT_SECRET_KEY"] = secrets.token_urlsafe(32)

    @app.before_request
    def enforce_local_demo_origin():
        # Do not trust forwarded-host headers: this runner is directly bound to
        # loopback and accepts exactly the authority shown to the user.
        if request.environ.get("HTTP_HOST") != SAFE_DEMO_AUTHORITY:
            return jsonify({
                "error": "host_not_allowed",
                "message": "Use the local Safe Demo address shown by the runner.",
            }), 403

        if request.path in SAFE_DEMO_POST_PATHS and request.method == "POST":
            if request.headers.get("Origin") != SAFE_DEMO_ORIGIN:
                return jsonify({
                    "error": "origin_not_allowed",
                    "message": "The request must come from the local Safe Demo page.",
                }), 403

            fetch_site = request.headers.get("Sec-Fetch-Site")
            if fetch_site is not None and fetch_site != "same-origin":
                return jsonify({
                    "error": "origin_not_allowed",
                    "message": "The request must come from the local Safe Demo page.",
                }), 403

    jwt = JWTManager()

    @jwt.unauthorized_loader
    def missing_token(_reason: str):
        return jsonify({"error": "unauthorized", "message": "Authentication is required."}), 401

    @jwt.invalid_token_loader
    def invalid_token(_reason: str):
        return jsonify({"error": "unauthorized", "message": "The access token is invalid."}), 401

    @jwt.expired_token_loader
    def expired_token(_header, _payload):
        return jsonify({"error": "unauthorized", "message": "The access token has expired."}), 401

    jwt.init_app(app)
    limiter.init_app(app)
    app.register_blueprint(lab_bootstrap_demo_bp)

    @app.errorhandler(429)
    def rate_limit_error(_error):
        return jsonify({
            "error": "rate_limit_exceeded",
            "message": "Too many requests. Slow down and try again in a moment.",
        }), 429

    return app


def main() -> None:
    app = create_safe_demo_app()
    app.run(
        host=SAFE_DEMO_HOST,
        port=SAFE_DEMO_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
        processes=1,
    )


if __name__ == "__main__":
    main()
