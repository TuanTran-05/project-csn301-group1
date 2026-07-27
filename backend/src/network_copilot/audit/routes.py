from datetime import datetime

from flask import Blueprint, jsonify, request

from ..auth.service import roles_required
from ..errors import ValidationError
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
    events = service.list_events(
        user_id=request.args.get("user_id", type=int),
        device_id=request.args.get("device_id", type=int),
        action=request.args.get("action"),
        result=request.args.get("result"),
        since=_timestamp_arg("since"),
        until=_timestamp_arg("until"),
        limit=request.args.get("limit", default=100, type=int),
    )
    return jsonify({"items": [event.to_dict() for event in events]}), 200
