"""APScheduler wiring for periodic device polling.

The scheduler only starts when MONITORING_ENABLED is true, so the test suite
never runs background jobs.
"""

import atexit
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .service import poll_all_enabled_devices

logger = logging.getLogger(__name__)

JOB_ID = "poll-all-devices"


def init_scheduler(app):
    """Start the polling job. Returns the scheduler, or None when disabled."""
    if not app.config.get("MONITORING_ENABLED"):
        logger.info("Monitoring scheduler disabled (MONITORING_ENABLED is false).")
        return None

    interval = app.config.get("MONITORING_INTERVAL_SECONDS", 60)
    scheduler = BackgroundScheduler(timezone="UTC")

    def run_poll():
        with app.app_context():
            try:
                snapshots = poll_all_enabled_devices()
                logger.info("Polled %d device(s).", len(snapshots))
            except Exception:  # pragma: no cover - a job must never die silently
                logger.exception("Device polling job failed.")

    scheduler.add_job(
        run_poll,
        "interval",
        seconds=interval,
        id=JOB_ID,
        # Never let a slow poll overlap the next one, and collapse any runs
        # missed while the previous poll was still going.
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    app.extensions["monitoring_scheduler"] = scheduler
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info("Monitoring scheduler started (every %ss).", interval)
    return scheduler
