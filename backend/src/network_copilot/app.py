from flask import Flask, jsonify

from .config import Config


def create_app(config_object: type | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object or Config)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    return app
