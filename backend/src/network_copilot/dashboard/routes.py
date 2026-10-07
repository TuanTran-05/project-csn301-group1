from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

from ..projects.scope import current_project
from . import service

bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")


@bp.get("/summary")
@jwt_required()
def summary():
    project = current_project()
    return jsonify(service.build_summary(project.id)), 200
