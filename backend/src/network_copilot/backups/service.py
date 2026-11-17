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
    """Save the device's first baseline if it has none; True when one exists.

    Without it a change to a PNETLab or physical device stops at approval
    with "capture a baseline", which a new user cannot guess. So NetAI saves
    one itself, when the device first answers on SSH (after the console
    bootstrap) and again, if still missing, before it previews a change.
    A device that cannot be reached is left as it is: the change is still
    refused at approval, exactly as before.
    """
    if not needs_baseline(device) or not device.managed:
        return True
    if db.session.query(ConfigBackup.id).filter_by(device_id=device.id).first() is not None:
        return True
    from ..governance.model import DeviceLease

    if db.session.get(DeviceLease, device.id) is not None:
        return False
    try:
        capture_backup(device, reason="approval_baseline")
    except Exception as exc:  # unreachable, no login yet: leave it to the manual button
        logger.warning("Could not save a baseline for %s: %s", device.hostname, exc)
        return False
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
