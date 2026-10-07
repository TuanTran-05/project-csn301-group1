from datetime import datetime

from flask import Blueprint, jsonify, request

from ..auth.service import roles_required
from ..errors import ValidationError
from ..projects.scope import current_project
from . import service

bp = Blueprint("audit", __name__, url_prefix="/api/audit-logs")


def _timestamp_arg(name: str) -> datetime | None:
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise ValidationError(
            f"'{name}' must be an ISO-8601 timestamp, for example 2026-07-27T09:00:00."
        ) from exc


@bp.get("")
@roles_required("ADMIN")
def list_audit_logs():
    # "scope=all" is the ADMIN-wide view: every project plus the events that
    # belong to none (logins, user administration).
    everything = request.args.get("scope") == "all"
    project_id = None if everything else current_project().id
    events = service.list_events(
        project_id=project_id,
        user_id=request.args.get("user_id", type=int),
        device_id=request.args.get("device_id", type=int),
        action=request.args.get("action"),
        result=request.args.get("result"),
        since=_timestamp_arg("since"),
        until=_timestamp_arg("until"),
        limit=request.args.get("limit", default=100, type=int),
    )
    return jsonify({"items": [event.to_dict() for event in events]}), 200
