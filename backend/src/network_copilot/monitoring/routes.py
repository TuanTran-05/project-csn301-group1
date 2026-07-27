from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..devices import service as device_service
from ..errors import ValidationError
from . import service

bp = Blueprint("monitoring", __name__, url_prefix="/api/devices")


def _status_payload(device, snapshot) -> dict:
    return {
        "device_id": device.id,
        "hostname": device.hostname,
        "role": device.role,
        "status": device.status,
        "monitoring_enabled": device.monitoring_enabled,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "snapshot": snapshot.to_dict() if snapshot else None,
    }


@bp.get("/<int:device_id>/status")
@jwt_required()
def device_status(device_id: int):
    device = device_service.get_device(device_id)
    return jsonify(_status_payload(device, service.latest_snapshot(device_id))), 200


@bp.get("/<int:device_id>/snapshots")
@jwt_required()
def device_snapshots(device_id: int):
    device_service.get_device(device_id)
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError as exc:
        raise ValidationError("'limit' must be an integer.") from exc

    snapshots = service.snapshot_history(device_id, limit=limit)
    return (
        jsonify({"items": [item.to_dict(include_raw=False) for item in snapshots]}),
        200,
    )


@bp.post("/<int:device_id>/refresh")
@jwt_required()
def refresh_device(device_id: int):
    device = device_service.get_device(device_id)
    snapshot = service.poll_device(device_id)
    return jsonify(_status_payload(device, snapshot)), 200
