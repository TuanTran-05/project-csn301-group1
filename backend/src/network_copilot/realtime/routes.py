import json
import threading
import time

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context
from flask_jwt_extended import jwt_required

from ..auth.service import current_user
from ..extensions import db
from ..projects import service as project_service
from . import state as project_state

bp = Blueprint("realtime", __name__, url_prefix="/api/projects/<int:project_id>")

_lock = threading.Lock()
_open_streams = 0


def _event(name: str, data) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


@bp.get("/state")
@jwt_required()
def get_state(project_id: int):
    """Fingerprints of the project's live data; 304 when nothing changed."""
    project_service.require_project(current_user(), project_id, "viewer")
    fingerprints = project_state.project_state(project_id)
    etag = project_state.combined(fingerprints)
    if request.if_none_match.contains(etag):
        response = Response(status=304)
    else:
        response = jsonify({"state": fingerprints})
    response.set_etag(etag)
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.get("/events")
@jwt_required()
def stream_events(project_id: int):
    """Server-sent events: one `state` event now and one on every change.

    A stream ends by itself after REALTIME_MAX_SECONDS (`bye`); the page just
    reconnects. A 503 tells the page to fall back to polling.
    """
    global _open_streams
    project_service.require_project(current_user(), project_id, "viewer")
    config = current_app.config
    if not config.get("REALTIME_ENABLED", True):
        return jsonify({"error": "realtime_disabled", "message": "Realtime updates are off."}), 503

    with _lock:
        if _open_streams >= config.get("REALTIME_MAX_STREAMS", 20):
            response = jsonify({"error": "too_many_streams", "message": "Too many open streams."})
            response.status_code = 503
            response.headers["Retry-After"] = "30"
            return response
        _open_streams += 1

    poll = float(config.get("REALTIME_POLL_SECONDS", 2))
    limit = float(config.get("REALTIME_MAX_SECONDS", 55))

    @stream_with_context
    def generate():
        global _open_streams
        try:
            started = last_beat = time.monotonic()
            last = None
            while True:
                current = project_state.project_state(project_id)
                db.session.rollback()  # end the read transaction between checks
                if current != last:
                    last = current
                    yield _event("state", current)
                    last_beat = time.monotonic()
                now = time.monotonic()
                if now - started >= limit:
                    yield _event("bye", {"reconnect": True})
                    return
                if now - last_beat >= 15:
                    yield ": keep-alive\n\n"
                    last_beat = now
                time.sleep(poll)
        finally:
            with _lock:
                _open_streams -= 1

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx must not hold the stream back
            "Connection": "keep-alive",
        },
    )
