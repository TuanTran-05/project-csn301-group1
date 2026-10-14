from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

from ..auth.service import roles_required
from ..projects.scope import current_project
from . import service

bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")


@bp.get("/monitoring")
@roles_required("ADMIN")
def monitoring_stats():
    """How the last polling cycle went (ADMIN)."""
    from flask import current_app

    enabled = bool(current_app.config.get("MONITORING_ENABLED"))
    stats = current_app.extensions.get("monitoring_stats")
    return jsonify({"enabled": enabled, "stats": stats}), 200


@bp.get("/summary")
@jwt_required()
def summary():
    project = current_project()
    return jsonify(service.build_summary(project.id)), 200
