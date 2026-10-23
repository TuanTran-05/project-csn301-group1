"""Standalone, inspection-only UI and API for local PNETLab ZIP analysis."""

from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore

from flask import (
    Blueprint,
    current_app,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
)
from flask_jwt_extended import create_access_token, jwt_required, set_access_cookies

from .extensions import limiter
from .lab_bootstrap.archive_inspection import inspect_archive_analysis
from .lab_bootstrap.pnetlab_archive import (
    IncompletePnetlabTopologyError,
    MAX_UPLOAD_BYTES,
    PnetlabArchiveError,
    UnsupportedPnetlabSchemaError,
    analyze_pnetlab_zip,
)
from .lab_bootstrap.safe_export import SafeExportError, build_safe_inspection_report_package

bp = Blueprint("lab_bootstrap_demo", __name__)
ARCHIVE_RATE_LIMIT = "5 per minute"
MAX_CONCURRENT_ARCHIVE_ANALYSES = 1
SAFE_DEMO_TOKEN_TTL = timedelta(minutes=5)
SAFE_DEMO_TOKEN_MAX_AGE_SECONDS = 5 * 60
_archive_analysis_slots = BoundedSemaphore(MAX_CONCURRENT_ARCHIVE_ANALYSES)


@bp.get("/lab-bootstrap-demo")
def page():
    """Serve the page and, only in the isolated runner, mint a short JWT cookie."""
    cookie_auth = bool(current_app.config.get("LAB_BOOTSTRAP_DEMO_COOKIE_AUTH", False))
    response = make_response(
        render_template(
            "lab_bootstrap_demo.html",
            cookie_auth=cookie_auth,
            csrf_cookie_name=current_app.config.get(
                "JWT_ACCESS_CSRF_COOKIE_NAME", ""
            ),
        )
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"

    if cookie_auth:
        token = create_access_token(
            identity="safe-demo",
            additional_claims={"demo_only": True},
            expires_delta=SAFE_DEMO_TOKEN_TTL,
        )
        set_access_cookies(
            response,
            token,
            max_age=SAFE_DEMO_TOKEN_MAX_AGE_SECONDS,
        )

    return response


@bp.get("/static/js/lab_bootstrap_demo.js")
def static_js():
    """Serve only the JavaScript asset required by the standalone page."""
    script_path = Path(__file__).resolve().parent / "static" / "js" / "lab_bootstrap_demo.js"
    return send_file(script_path, mimetype="text/javascript", conditional=True)


@bp.post("/api/lab-bootstrap-demo/analyze-archive")
@jwt_required()
@limiter.limit(ARCHIVE_RATE_LIMIT)
def analyze_archive():
    """Read one bounded raw ZIP body and return server-derived inspection only."""
    if request.mimetype != "application/zip":
        return jsonify({
            "error": "unsupported_media_type",
            "message": "Send the ZIP file as application/zip.",
        }), 415

    if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
        return jsonify({
            "error": "upload_too_large",
            "message": "The ZIP exceeds the 10 MiB upload limit.",
        }), 413

    if not _archive_analysis_slots.acquire(blocking=False):
        return jsonify({
            "error": "analysis_busy",
            "message": "A ZIP analysis is already in progress. Try again shortly.",
        }), 503

    try:
        return _analyze_archive_request()
    finally:
        # This is process-local capacity for the Safe Demo's threaded runner.
        # Multi-worker deployments need a shared coordinator for a global cap.
        _archive_analysis_slots.release()


@bp.post("/api/lab-bootstrap-demo/export-inspection")
@jwt_required()
@limiter.limit(ARCHIVE_RATE_LIMIT)
def export_inspection():
    """Export a counts-and-checksums report after a clean offline inspection."""
    if request.mimetype != "application/zip":
        return jsonify({
            "error": "unsupported_media_type",
            "message": "Send the ZIP file as application/zip.",
        }), 415

    if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
        return jsonify({
            "error": "upload_too_large",
            "message": "The ZIP exceeds the 10 MiB upload limit.",
        }), 413

    if not _archive_analysis_slots.acquire(blocking=False):
        return jsonify({
            "error": "analysis_busy",
            "message": "A ZIP analysis is already in progress. Try again shortly.",
        }), 503

    try:
        archive_bytes, read_error = _read_archive_body()
        if read_error is not None:
            return read_error

        analysis, inspection, analysis_error = _analyze_archive_bytes(archive_bytes)
        if analysis_error is not None:
            return analysis_error

        try:
            package = build_safe_inspection_report_package(analysis, inspection)
        except SafeExportError:
            return jsonify({
                "error": "export_not_supported",
                "message": "Inspection issues or unsupported state prevent creating a review-only report.",
            }), 422
        except Exception:
            return jsonify({
                "error": "export_failed",
                "message": "The review-only report could not be created safely.",
            }), 500

        response = send_file(
            BytesIO(package),
            mimetype="application/zip",
            as_attachment=True,
            download_name="lab-bootstrap-inspection-report.zip",
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    finally:
        # The same process-local slot caps ZIP parsing for both demo actions.
        _archive_analysis_slots.release()


def _analyze_archive_request():
    archive_bytes, read_error = _read_archive_body()
    if read_error is not None:
        return read_error

    analysis, inspection, analysis_error = _analyze_archive_bytes(archive_bytes)
    if analysis_error is not None:
        return analysis_error

    planner_blockers = list(inspection["blockers"]) + list(inspection["issues"])
    if not planner_blockers:
        planner_blockers.append(
            "This Safe Demo endpoint performs inspection only and never invokes the IP planner."
        )

    return jsonify({
        "status": analysis["status"],
        "analysis": analysis,
        "inspection": inspection,
        "preview_compatible": analysis["preview_compatible"],
        "planner_eligible": inspection["planner_eligible"],
        "planner_status": "NOT_RUN",
        "planner_blockers": planner_blockers,
        "gateway_status": "NOT_SUPPORTED",
        "device_actions": "NOT_PERFORMED",
    }), 200


def _read_archive_body():
    try:
        archive_bytes = request.stream.read(MAX_UPLOAD_BYTES + 1)
    except Exception:
        return None, (jsonify({
            "error": "invalid_upload",
            "message": "The ZIP upload could not be read.",
        }), 400)

    if len(archive_bytes) > MAX_UPLOAD_BYTES:
        return None, (jsonify({
            "error": "upload_too_large",
            "message": "The ZIP exceeds the 10 MiB upload limit.",
        }), 413)
    return archive_bytes, None


def _analyze_archive_bytes(archive_bytes):
    try:
        # The filename is intentionally server-fixed. Client-supplied inspection,
        # compatibility, and planner fields are never accepted or consulted.
        analysis = analyze_pnetlab_zip("safe-demo-upload.zip", archive_bytes)
        inspection = inspect_archive_analysis(analysis)
    except UnsupportedPnetlabSchemaError as exc:
        return None, None, (jsonify({"error": "unsupported_schema", "message": str(exc)}), 422)
    except IncompletePnetlabTopologyError as exc:
        return None, None, (jsonify({"error": "incomplete_topology", "message": str(exc)}), 422)
    except PnetlabArchiveError as exc:
        return None, None, (jsonify({"error": "invalid_archive", "message": str(exc)}), 400)
    except Exception:
        # Do not return parser internals, local paths, or stack traces.
        return None, None, (jsonify({
            "error": "analysis_failed",
            "message": "The archive could not be analyzed safely.",
        }), 500)
    return analysis, inspection, None
