"""Running-config backups. Always taken before a change is applied."""

import logging

from ..devices.model import Device
from ..extensions import db
from ..ssh.client import build_client_for_device
from .model import ConfigBackup

logger = logging.getLogger(__name__)

BACKUP_COMMAND = "show running-config"


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
    result = client.run_show(BACKUP_COMMAND)

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
