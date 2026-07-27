from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..audit.service import record_event
from ..auth.service import current_user, roles_required
from . import service

bp = Blueprint("devices", __name__, url_prefix="/api/devices")


def _current_user_id() -> int | None:
    user = current_user()
    return user.id if user else None


@bp.get("")
@jwt_required()
def list_devices():
    devices = service.list_devices(
        role=request.args.get("role"), status=request.args.get("status")
    )
    return jsonify({"items": [device.to_dict() for device in devices]}), 200


@bp.post("")
@roles_required("ADMIN")
def create_device():
    device = service.create_device(request.get_json(silent=True) or {})
    record_event(
        action="device.create",
        result="success",
        user_id=_current_user_id(),
        device_id=device.id,
        details=device.to_dict(),
    )
    return jsonify(device.to_dict()), 201


@bp.get("/<int:device_id>")
@jwt_required()
def get_device(device_id: int):
    return jsonify(service.get_device(device_id).to_dict()), 200


@bp.put("/<int:device_id>")
@roles_required("ADMIN")
def update_device(device_id: int):
    payload = request.get_json(silent=True) or {}
    device = service.update_device(device_id, payload)
    record_event(
        action="device.update",
        result="success",
        user_id=_current_user_id(),
        device_id=device.id,
        details={"changes": payload},
    )
    return jsonify(device.to_dict()), 200


@bp.delete("/<int:device_id>")
@roles_required("ADMIN")
def delete_device(device_id: int):
    device = service.get_device(device_id)
    hostname = device.hostname
    service.delete_device(device_id)
    record_event(
        action="device.delete",
        result="success",
        user_id=_current_user_id(),
        details={"device_id": device_id, "hostname": hostname},
    )
    return "", 204


@bp.get("/<int:device_id>/backups")
@jwt_required()
def list_backups(device_id: int):
    from ..backups import service as backup_service

    service.get_device(device_id)
    backups = backup_service.list_backups(
        device_id, limit=request.args.get("limit", default=50, type=int)
    )
    return jsonify({"items": [backup.to_dict() for backup in backups]}), 200


@bp.get("/<int:device_id>/backups/<int:backup_id>")
@roles_required("ADMIN")
def get_backup(device_id: int, backup_id: int):
    from ..backups import service as backup_service
    from ..errors import NotFoundError

    service.get_device(device_id)
    backup = backup_service.get_backup(backup_id)
    if backup is None or backup.device_id != device_id:
        raise NotFoundError(f"Backup {backup_id} was not found.")
    return jsonify(backup.to_dict(include_config=True)), 200


@bp.post("/<int:device_id>/test-connection")
@roles_required("ADMIN")
def test_connection(device_id: int):
    device = service.get_device(device_id)
    reachable, detail = service.check_reachability(device)
    record_event(
        action="device.test_connection",
        result="success" if reachable else "failure",
        user_id=_current_user_id(),
        device_id=device.id,
        message=detail,
        details={"hostname": device.hostname, "status": device.status},
    )
    return (
        jsonify(
            {
                "device_id": device.id,
                "hostname": device.hostname,
                "reachable": reachable,
                "status": device.status,
                "detail": detail,
            }
        ),
        200,
    )
