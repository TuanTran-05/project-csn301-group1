"""Running-config backups. Always taken before a change is applied."""

import logging

from ..devices.model import Device
from ..extensions import db
from ..ssh.client import build_client_for_device
from .model import ConfigBackup

logger = logging.getLogger(__name__)

BACKUP_COMMAND = "show running-config"
# PAN-OS spells it differently; the output is the XML-style config tree.
PANOS_BACKUP_COMMAND = "show config running"


def needs_baseline(device: Device) -> bool:
    """Whether a change to the device is approved only against a saved baseline.

    The same rule as governance.workflow.deployment_allowed: every device but
    an isolated virtual one.
    """
    return device.environment != "virtual" or not device.isolated


def ensure_baseline(device: Device) -> bool:
    """Make the newest saved config match the device; True when a baseline exists.

    A change to a PNETLab or physical device is frozen against the newest
    backup at preview, and the apply refuses when the device no longer
    matches it. Saving only a *first* baseline was not enough: once the
    device changed outside NetAI (console, reload, a failed post-change
    backup) that old backup was stale and every change stopped with
    "Running configuration changed since preview" until the user pressed
    "Lưu cấu hình nền" by hand. So NetAI reads the running config before it
    previews a change and stores it when it differs from the newest backup.
    A device that cannot be reached keeps whatever baseline it has.
    """
    if not needs_baseline(device) or not device.managed:
        return True
    latest = (
        db.session.query(ConfigBackup)
        .filter_by(device_id=device.id)
        .order_by(ConfigBackup.id.desc())
        .first()
    )
    from ..governance.model import DeviceLease

    if db.session.get(DeviceLease, device.id) is not None:
        return latest is not None
    try:
        output = build_client_for_device(device).run_show(backup_command_for(device)).output
    except Exception as exc:  # unreachable, no login yet: leave it to the manual button
        logger.warning("Could not save a baseline for %s: %s", device.hostname, exc)
        return latest is not None
    from ..governance.workflow import config_hash

    if latest is not None and latest.running_config is not None and \
            config_hash(latest.running_config) == config_hash(output or ""):
        return True
    db.session.add(ConfigBackup(device_id=device.id, running_config=output, reason="approval_baseline"))
    db.session.commit()
    logger.info("Stored approval_baseline backup for %s.", device.hostname)
    return True


def backup_command_for(device: Device) -> str:
    return PANOS_BACKUP_COMMAND if device.device_type == "paloalto_panos" else BACKUP_COMMAND


def capture_backup(
    device: Device,
    change_request_id: int | None = None,
    reason: str = "pre_change",
    client=None,
) -> ConfigBackup:
    """Fetch and store the running config.

    Propagates SSHError so a caller can abort before touching configuration:
    a change must never be applied without a backup.
    """
    client = client or build_client_for_device(device)
    result = client.run_show(backup_command_for(device))

    backup = ConfigBackup(
        device_id=device.id,
        change_request_id=change_request_id,
        running_config=result.output,
        reason=reason,
    )
    db.session.add(backup)
    db.session.commit()
    logger.info(
        "Stored %s backup for %s (%d bytes).",
        reason,
        device.hostname,
        len(result.output or ""),
    )
    return backup


def list_backups(device_id: int, limit: int = 50) -> list[ConfigBackup]:
    return (
        db.session.query(ConfigBackup)
        .filter(ConfigBackup.device_id == device_id)
        .order_by(ConfigBackup.created_at.desc(), ConfigBackup.id.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )


def get_backup(backup_id: int) -> ConfigBackup | None:
    return db.session.get(ConfigBackup, backup_id)
