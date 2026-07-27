from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from ..auth.service import roles_required
from . import service

bp = Blueprint("devices", __name__, url_prefix="/api/devices")


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
    return jsonify(device.to_dict()), 201


@bp.get("/<int:device_id>")
@jwt_required()
def get_device(device_id: int):
    return jsonify(service.get_device(device_id).to_dict()), 200


@bp.put("/<int:device_id>")
@roles_required("ADMIN")
def update_device(device_id: int):
    device = service.update_device(device_id, request.get_json(silent=True) or {})
    return jsonify(device.to_dict()), 200


@bp.delete("/<int:device_id>")
@roles_required("ADMIN")
def delete_device(device_id: int):
    service.delete_device(device_id)
    return "", 204


@bp.post("/<int:device_id>/test-connection")
@roles_required("ADMIN")
def test_connection(device_id: int):
    device = service.get_device(device_id)
    reachable, detail = service.check_reachability(device)
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
