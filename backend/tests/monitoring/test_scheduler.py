from network_copilot.app import create_app
from network_copilot.config import TestConfig
from network_copilot.monitoring import scheduler as scheduler_module


class RecordingScheduler:
    def __init__(self):
        self.jobs = []
        self.started = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.started = False


def test_scheduler_is_not_started_during_tests(app):
    assert app.extensions.get("monitoring_scheduler") is None


def test_scheduler_starts_when_monitoring_is_enabled(monkeypatch):
    recorder = RecordingScheduler()
    monkeypatch.setattr(
        scheduler_module, "BackgroundScheduler", lambda *a, **kw: recorder
    )

    class EnabledConfig(TestConfig):
        MONITORING_ENABLED = True

    app = create_app(EnabledConfig)
    assert app.extensions["monitoring_scheduler"] is recorder
    assert recorder.started is True


def test_polling_job_uses_the_configured_interval(monkeypatch):
    recorder = RecordingScheduler()
    monkeypatch.setattr(
        scheduler_module, "BackgroundScheduler", lambda *a, **kw: recorder
    )

    class EnabledConfig(TestConfig):
        MONITORING_ENABLED = True
        MONITORING_INTERVAL_SECONDS = 60

    create_app(EnabledConfig)
    job = recorder.jobs[0]
    assert job["trigger"] == "interval"
    assert job["seconds"] == 60


def test_polling_job_does_not_overlap_or_pile_up(monkeypatch):
    recorder = RecordingScheduler()
    monkeypatch.setattr(
        scheduler_module, "BackgroundScheduler", lambda *a, **kw: recorder
    )

    class EnabledConfig(TestConfig):
        MONITORING_ENABLED = True

    create_app(EnabledConfig)
    job = recorder.jobs[0]
    assert job["max_instances"] == 1
    assert job["coalesce"] is True


def test_job_runs_inside_an_application_context(monkeypatch):
    recorder = RecordingScheduler()
    monkeypatch.setattr(
        scheduler_module, "BackgroundScheduler", lambda *a, **kw: recorder
    )
    calls = []
    monkeypatch.setattr(
        scheduler_module, "poll_all_enabled_devices", lambda: calls.append(True)
    )

    class EnabledConfig(TestConfig):
        MONITORING_ENABLED = True

    app = create_app(EnabledConfig)
    with app.app_context():
        from network_copilot.extensions import db

        db.create_all()
    recorder.jobs[0]["func"]()
    assert calls == [True]
