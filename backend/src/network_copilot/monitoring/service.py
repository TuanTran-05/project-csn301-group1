"""Device monitoring: poll read-only state and store snapshots."""

import logging
import time

from ..devices import service as device_service
from ..devices.model import Device
from ..extensions import db
from ..parsers import parse_command_output
from ..ssh.client import build_client_for_device
from ..ssh.exceptions import SSHError
from .model import DeviceSnapshot

logger = logging.getLogger(__name__)

IOS_BASE_COMMANDS = ["show ip interface brief", "show ip route"]
# Cisco ASA uses a different vocabulary for the same two questions.
ASA_BASE_COMMANDS = ["show interface ip brief", "show route"]
ASA_DEVICE_TYPES = {"cisco_asa"}

# Kept as the IOS base so existing callers and tests are unaffected.
BASE_COMMANDS = IOS_BASE_COMMANDS

ROUTING_ROLES = {"core", "distribution"}
SWITCHING_ROLES = {"access", "distribution"}


def _role_extras(role: str) -> list[str]:
    """Role-driven additions, identical for every device type."""
    extras: list[str] = []
    if role in ROUTING_ROLES:
        extras.append("show ip ospf neighbor")
    if role in SWITCHING_ROLES:
        extras.append("show vlan brief")
        extras.append("show interfaces trunk")
    if role in ROUTING_ROLES:
        extras.append("show ip dhcp pool")
    return extras


def commands_for_role(role: str) -> list[str]:
    """Read-only IOS commands to poll for a device in the given role."""
    return list(IOS_BASE_COMMANDS) + _role_extras(role)


def commands_for_device(device: Device) -> list[str]:
    """Read-only commands to poll for one device, honouring its type.

    This is the one place that genuinely needs device_type: it chooses
    commands with no model in the loop, so nothing else can catch a
    wrong-vendor spelling before it reaches the device.
    """
    base = (
        ASA_BASE_COMMANDS
        if device.device_type in ASA_DEVICE_TYPES
        else IOS_BASE_COMMANDS
    )
    return list(base) + _role_extras(device.role)


def _save(
    device: Device,
    status: str,
    raw_output: dict,
    parsed_data: dict,
    error: str | None,
    duration_ms: int,
) -> DeviceSnapshot:
    snapshot = DeviceSnapshot(
        device_id=device.id,
        status=status,
        raw_output=raw_output,
        parsed_data=parsed_data,
        error=error,
        duration_ms=duration_ms,
    )
    db.session.add(snapshot)
    device_service.set_device_status(device, status)
    db.session.commit()
    return snapshot


def _collect(device: Device, client, started: float) -> tuple:
    """Run the poll commands and parse them. Touches no database, so it can run
    in a worker thread. Returns (status, raw, parsed, error, elapsed_ms)."""
    raw_output: dict[str, str] = {}
    parsed_data: dict[str, list] = {}
    try:
        for command in commands_for_device(device):
            result = client.run_show(command)
            raw_output[command] = result.output
            parsed = parse_command_output(command, result.output)
            if parsed is not None:
                parsed_data[command] = parsed
    except SSHError as exc:
        logger.info("Poll failed for %s: %s", device.hostname, exc.message)
        return "offline", raw_output, parsed_data, exc.message, _elapsed_ms(started)
    except Exception as exc:  # pragma: no cover - defensive: a poll must not crash
        logger.exception("Unexpected error polling %s", device.hostname)
        return "offline", raw_output, parsed_data, f"{type(exc).__name__}", _elapsed_ms(started)
    return "online", raw_output, parsed_data, None, _elapsed_ms(started)


def poll_device(device_id: int) -> DeviceSnapshot:
    """Poll one device and persist the result. Never raises on SSH failure."""
    device = device_service.get_device(device_id)
    started = time.monotonic()
    try:
        client = build_client_for_device(device)
    except SSHError as exc:
        return _save(device, "offline", {}, {}, exc.message, _elapsed_ms(started))
    return _save(device, *_collect(device, client, started))


def poll_all_enabled_devices(max_workers: int | None = None) -> list[DeviceSnapshot]:
    """Poll every monitored device, a few at a time. One failure never stops the rest.

    The slow part is SSH, so it runs in up to ``max_workers`` threads (config
    ``MONITORING_MAX_WORKERS``); clients are built and results saved on the
    calling thread, which owns the database session.
    """
    from flask import current_app, has_app_context

    if max_workers is None:
        max_workers = (
            current_app.config.get("MONITORING_MAX_WORKERS", 4) if has_app_context() else 4
        )
    cycle_started = time.monotonic()
    devices = (
        db.session.query(Device)
        .filter(Device.monitoring_enabled.is_(True))
        .order_by(Device.id)
        .all()
    )

    prepared, snapshots = [], []
    for device in devices:
        started = time.monotonic()
        try:
            prepared.append((device, build_client_for_device(device), started))
        except SSHError as exc:
            snapshots.append(_save(device, "offline", {}, {}, exc.message, _elapsed_ms(started)))
        except Exception:  # pragma: no cover - e.g. no credential stored
            logger.exception("Skipping %s after an unexpected error", device.hostname)

    workers = max(1, min(int(max_workers or 1), len(prepared) or 1))
    if workers == 1:
        collected = [_collect(*item) for item in prepared]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            collected = list(pool.map(lambda item: _collect(*item), prepared))

    for (device, _client, _started), outcome in zip(prepared, collected):
        try:
            snapshots.append(_save(device, *outcome))
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not save the poll of %s", device.hostname)
            db.session.rollback()

    _record_cycle(len(devices), workers, time.monotonic() - cycle_started)
    return snapshots


def _record_cycle(device_count: int, workers: int, seconds: float) -> None:
    """Remember how long a cycle took, and say so when it outlasts the interval."""
    from datetime import datetime, timezone

    from flask import current_app, has_app_context

    if not has_app_context():
        return
    interval = current_app.config.get("MONITORING_INTERVAL_SECONDS", 60)
    current_app.extensions["monitoring_stats"] = {
        "devices": device_count,
        "workers": workers,
        "last_cycle_seconds": round(seconds, 2),
        "interval_seconds": interval,
        "overrunning": seconds > interval,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if seconds > interval:
        logger.warning(
            "Polling %d device(s) took %.1fs, longer than the %ss interval; "
            "raise MONITORING_MAX_WORKERS or the interval.",
            device_count, seconds, interval,
        )


def latest_snapshot(device_id: int) -> DeviceSnapshot | None:
    return (
        db.session.query(DeviceSnapshot)
        .filter(DeviceSnapshot.device_id == device_id)
        .order_by(DeviceSnapshot.created_at.desc(), DeviceSnapshot.id.desc())
        .first()
    )


def snapshot_history(device_id: int, limit: int = 50) -> list[DeviceSnapshot]:
    return (
        db.session.query(DeviceSnapshot)
        .filter(DeviceSnapshot.device_id == device_id)
        .order_by(DeviceSnapshot.created_at.desc(), DeviceSnapshot.id.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
