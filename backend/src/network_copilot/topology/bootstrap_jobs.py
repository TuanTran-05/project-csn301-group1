"""Console bootstrap as a background job the page polls.

Booting a PA-VM and committing on it take minutes, longer than a reverse
proxy (or Gunicorn) lets one HTTP request live. ``start`` returns at once
with a job id; a thread does the work and the page asks ``get`` for progress.

Jobs live in this process's memory: the service runs one Gunicorn worker
(threads serve the requests), so every request sees the same jobs. A job
holds only what the page shows - results and masked steps - never the
request payload with its passwords. Finished jobs are dropped after a day.
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from ..errors import NotFoundError

logger = logging.getLogger(__name__)

KEEP_SECONDS = 24 * 3600
MAX_STEPS = 400

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prune() -> None:
    cutoff = time.time() - KEEP_SECONDS
    for job_id in [k for k, job in _jobs.items() if job["_finished"] and job["_finished"] < cutoff]:
        del _jobs[job_id]


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _update(job_id: str, **changes) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(changes, updated_at=_now())


def _progress(job_id: str, hostname=None, step=None, **update) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        if step is not None and len(job["log"]) < MAX_STEPS:
            job["log"].append({"hostname": hostname, **step})
        if update:
            job["current"] = {"hostname": hostname, **update}
        job["updated_at"] = _now()


def start(app, project_id: int, user_id: int | None, payload: dict, work,
          action: str = "topology.pnetlab_bootstrap") -> dict:
    """Create a job and run ``work(project, payload, progress)`` for it.

    ``work`` returns the result dict ({"devices": [...], ...}); its devices
    become the job's. With ``BOOTSTRAP_RUN_INLINE`` (tests) it runs before
    this returns.
    """
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id, "project_id": project_id, "status": "queued",
        "created_at": _now(), "updated_at": _now(),
        "current": None, "devices": [], "log": [], "message": None, "summary": None,
        "_user_id": user_id, "_finished": None,
    }
    with _lock:
        _prune()
        _jobs[job_id] = job

    def target():
        with app.app_context():
            _run(app, job_id, project_id, user_id, payload, work, action)

    if app.config.get("BOOTSTRAP_RUN_INLINE"):
        target()
    else:
        threading.Thread(target=target, name=f"bootstrap-{job_id[:8]}", daemon=True).start()
    return get(job_id, project_id)


def _run(app, job_id, project_id, user_id, payload, work, action) -> None:
    from ..audit.service import record_event
    from ..extensions import db
    from ..projects.model import Project

    _update(job_id, status="running")
    result, status, message = None, "done", None
    try:
        project = db.session.get(Project, project_id)
        result = work(project, payload, lambda **kw: _progress(job_id, **kw))
    except Exception as exc:  # the job must always end
        message = getattr(exc, "message", None) or "The bootstrap job failed unexpectedly."
        status = "error"
        logger.exception("Bootstrap job %s failed", job_id)
    finally:
        if not app.config.get("BOOTSTRAP_RUN_INLINE"):
            db.session.remove()  # this thread's own session
    devices = (result or {}).get("devices", [])
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(status=status, message=message, devices=devices, current=None,
                       api=(result or {}).get("api"), summary=(result or {}).get("summary"),
                       updated_at=_now(), _finished=time.time())
    record_event(
        action=action,
        result="success" if status == "done" and all(
            d.get("status") in ("ok", "skipped") for d in devices) else "failure",
        user_id=user_id,
        project_id=project_id,
        details={
            "job": job_id, "lab": payload.get("lab_name") or payload.get("lab"), "url": payload.get("url"),
            "overwrite": bool(payload.get("overwrite")),
            "devices": [{"hostname": d.get("hostname"), "status": d.get("status"),
                         "interface": d.get("interface")} for d in devices],
        },
    )


def get(job_id: str, project_id: int) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job["project_id"] != project_id:
            raise NotFoundError("This bootstrap job does not exist (or the service restarted).")
        return _public(dict(job, log=list(job["log"]), devices=list(job["devices"])))
