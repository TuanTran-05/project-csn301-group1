"""History endpoints for database-backed unapproved previews; never SSH."""
import json
from functools import wraps
from threading import RLock

from flask import Blueprint, Response, g, jsonify, request

from ..auth.service import current_user, roles_required
from ..errors import AppError, ForbiddenError, ValidationError
from ..extensions import db
from .pending_service import workspace
from .protection import BackupSizeError, MAX_BACKUP_BYTES


def init_app(app):
    if "pending_history" in app.blueprints:
        return
    app.extensions["pending_history"] = {"users": {}, "lock": RLock()}
    request_lock = RLock()
    app.extensions["pending_history"]["request_lock"] = request_lock

    @app.before_request
    def serialize_pending_lifecycle():
        # Also serialize approval's read/commit, so an in-process edit cannot slip
        # between those steps. Existing lifecycle/authorization behavior is unchanged.
        critical = request.path.startswith("/api/pending-history") or (
            request.method in {"POST", "PATCH"} and (request.path.startswith(
                ("/api/changes", "/api/change-batches")) or request.path == "/api/ai/chat"))
        if critical:
            request_lock.acquire()
            g.pending_history_request_locked = True

    @app.teardown_request
    def release_pending_lifecycle(exc):
        if g.pop("pending_history_request_locked", False):
            request_lock.release()
    bp = Blueprint("pending_history", __name__, url_prefix="/api/pending-history")

    @bp.before_request
    @roles_required("ADMIN")
    def authorize():
        user = current_user()
        if user is None or not user.is_active or user.role != "ADMIN":
            raise ForbiddenError("Active ADMIN required")
        if request.args.get("topology_id", "default") != "default":
            raise ValidationError("Pending database has no topology mapping; only default is supported")

    def context():
        user = current_user()
        return workspace(user.id), user.username

    def payload():
        raw = request.stream.read(MAX_BACKUP_BYTES + 1)
        if len(raw) > MAX_BACKUP_BYTES:
            raise BackupSizeError("Backup exceeds the restore size limit")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValidationError("Invalid JSON") from exc
        if not isinstance(data, dict):
            raise ValidationError("Expected a JSON object")
        return data

    @bp.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error="invalid_history", message="Invalid backup or history data"), 400

    def protected_endpoint(fn):
        # Authorization stays outside this wrapper so JWT's own handlers still run.
        @wraps(fn)
        def safe(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except AppError as exc:
                db.session.rollback()
                messages = {409: "Pending draft changed; reopen the editor or refresh history",
                            422: "Invalid pending draft request", 403: "Pending draft request rejected"}
                details = {key: value for key, value in exc.details.items()
                           if key in {"reason", "reload_required", "revision"} or
                           (key.startswith("changes.") and key.endswith(".id"))}
                result = {"error": exc.error, "message": messages.get(exc.status_code, "Pending request failed")}
                if details:
                    result["details"] = details
                return jsonify(result), exc.status_code
            except BackupSizeError:
                db.session.rollback()
                return jsonify(error="backup_too_large", message="Backup exceeds the 2 MiB restore limit"), 422
            except ValueError:
                db.session.rollback()
                return jsonify(error="invalid_history", message="Invalid backup or history data"), 400
            except Exception as exc:
                db.session.rollback()
                # Never log exception text/tracebacks: SQL parameters can hold secrets.
                app.logger.error("Pending history request failed (%s)", type(exc).__name__)
                return jsonify(error="internal_error", message="Pending history request failed"), 500
        return safe

    @bp.get("")
    @protected_endpoint
    def status():
        work, actor = context()
        return jsonify(work.status(actor))

    @bp.post("/undo")
    @protected_endpoint
    def undo():
        work, actor = context()
        return jsonify(work.move(actor))

    @bp.post("/redo")
    @protected_endpoint
    def redo():
        work, actor = context()
        return jsonify(work.move(actor, redo=True))

    @bp.get("/backup")
    @protected_endpoint
    def backup():
        work, actor = context()
        with work.lock:
            work.status(actor)
            data = work.history.export_backup_file()
        return Response(data, mimetype="application/json", headers={
            "Content-Disposition": 'attachment; filename="pending-history.json"'})

    @bp.post("/restore")
    @protected_endpoint
    def restore():
        work, actor = context()
        return jsonify(work.move(actor, backup=payload()))

    @bp.patch("/changes/<int:record_id>")
    @protected_endpoint
    def edit_change(record_id):
        work, actor = context()
        return jsonify(work.edit(actor, "change", record_id, payload()))

    @bp.patch("/batches/<int:record_id>")
    @protected_endpoint
    def edit_batch(record_id):
        work, actor = context()
        return jsonify(work.edit(actor, "batch", record_id, payload()))

    @bp.get("/changes/<int:record_id>/editor")
    @protected_endpoint
    def change_editor(record_id):
        work, _ = context()
        return jsonify(work.editor("change", record_id))

    @bp.get("/batches/<int:record_id>/editor")
    @protected_endpoint
    def batch_editor(record_id):
        work, _ = context()
        return jsonify(work.editor("batch", record_id))

    @bp.get("/audit")
    @protected_endpoint
    def audit():
        work, actor = context()
        with work.lock:
            work.status(actor)
            return jsonify(work.history.export_audit_log())

    app.register_blueprint(bp)
