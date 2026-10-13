"""Optional JWT-protected API for user-owned draft history, never device writes."""

from threading import RLock

from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity

from ..auth.service import current_user, roles_required
from .history_manager import HistoryManager
from .protection import BackupSizeError, MAX_BACKUP_BYTES


def init_app(app):
    """Register once per Flask app. Storage lifetime is one server process."""
    from .pending_routes import init_app as init_pending_history
    init_pending_history(app)
    if "draft_history" in app.blueprints:
        return
    managers, lock = {}, RLock()
    bp = Blueprint("draft_history", __name__, url_prefix="/api/draft-history")

    @bp.before_request
    @roles_required("ADMIN")
    def authorize():
        user = current_user()
        if user is None or not user.is_active or user.role != "ADMIN":
            return jsonify(error="forbidden", message="Active ADMIN required"), 403

    def manager():
        with lock:
            return managers.setdefault(get_jwt_identity(), HistoryManager())

    def topology():
        value = request.args.get("topology_id", "default")
        if not value.strip() or len(value) > 128:
            raise ValueError("Invalid topology_id")
        return value

    def payload():
        # Bound only this extension, not the application's existing configuration.
        raw = request.stream.read(MAX_BACKUP_BYTES + 1)
        if len(raw) > MAX_BACKUP_BYTES:
            raise BackupSizeError("Backup exceeds the restore size limit")
        import json
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    @bp.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error="invalid_history", message="Invalid history data or unavailable backup key"), 400

    @bp.errorhandler(BackupSizeError)
    def too_large(exc):
        return jsonify(error="backup_too_large", message="Backup exceeds the 2 MiB restore limit"), 422

    @bp.get("")
    def status():
        return jsonify(manager().status(topology()))

    @bp.post("/states")
    def push():
        data, tid = payload(), topology()
        manager().push_state(data.get("snapshot"), get_jwt().get("username", get_jwt_identity()),
                             data.get("action_type"), data.get("description"), tid)
        return jsonify(manager().status(tid)), 201

    @bp.post("/undo")
    def undo():
        tid = topology()
        if manager().undo(tid, actor=get_jwt().get("username", get_jwt_identity())) is None:
            return jsonify(error="invalid_state", message="No previous draft"), 409
        return jsonify(manager().status(tid))

    @bp.post("/redo")
    def redo():
        tid = topology()
        if manager().redo(tid, actor=get_jwt().get("username", get_jwt_identity())) is None:
            return jsonify(error="invalid_state", message="No next draft"), 409
        return jsonify(manager().status(tid))

    @bp.get("/backup")
    def backup():
        return Response(manager().export_backup_file(topology()), mimetype="application/json",
                        headers={"Content-Disposition": 'attachment; filename="draft-history.json"'})

    @bp.post("/restore")
    def restore():
        tid = topology()
        manager().restore_from_backup(payload(), tid, actor=get_jwt().get("username", get_jwt_identity()))
        return jsonify(manager().status(tid))

    app.register_blueprint(bp)
