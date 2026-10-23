from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..audit import service as audit_service
from ..auth.service import roles_required
from ..errors import ValidationError
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


@bp.get("/activity")
@jwt_required()
def activity():
    """Return a cursor-paginated audit history for the selected project."""
    project = current_project()
    page_size = min(
        max(request.args.get("limit", default=50, type=int) or 50, 1),
        100,
    )
    before_raw = request.args.get("before")
    before_id = request.args.get("before_id", type=int)
    before = None

    if before_raw or before_id is not None:
        if not before_raw or before_id is None or before_id < 1:
            raise ValidationError(
                "'before' and a positive 'before_id' must be provided together."
            )
        try:
            before = datetime.fromisoformat(before_raw.replace("Z", "+00:00"))
            if before.tzinfo is not None:
                before = before.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError as exc:
            raise ValidationError("'before' must be an ISO-8601 timestamp.") from exc

    rows = audit_service.list_events(
        project_id=project.id,
        limit=page_size + 1,
        before_created_at=before,
        before_id=before_id,
    )
    has_more = len(rows) > page_size
    return jsonify(
        {
            "items": [event.to_dict() for event in rows[:page_size]],
            "has_more": has_more,
        }
    ), 200
