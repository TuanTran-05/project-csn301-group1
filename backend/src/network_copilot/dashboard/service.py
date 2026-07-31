"""Aggregate existing device/monitoring/changes/audit data for the dashboard."""

from ..devices import service as device_service
from ..monitoring.service import ROUTING_ROLES, latest_snapshot

OSPF_COMMAND = "show ip ospf neighbor"


def _device_role_rollup(devices) -> dict:
    rollup: dict[str, dict[str, int]] = {}
    for device in devices:
        bucket = rollup.setdefault(
            device.role, {"total": 0, "online": 0, "offline": 0, "unknown": 0}
        )
        bucket["total"] += 1
        bucket[device.status] += 1
    return rollup


def _ospf_health(snapshot) -> tuple[str, list[dict]]:
    if snapshot is None or snapshot.status != "online":
        return "no_data", []
    neighbors = snapshot.parsed_data.get(OSPF_COMMAND)
    if neighbors is None:
        return "no_data", []
    if len(neighbors) == 0:
        return "down", []
    if all("FULL" in neighbor["state"] for neighbor in neighbors):
        return "ok", neighbors
    return "degraded", neighbors


def _ospf_entry(device) -> dict:
    snapshot = latest_snapshot(device.id)
    health, neighbors = _ospf_health(snapshot)
    full_count = sum(1 for neighbor in neighbors if "FULL" in neighbor["state"])
    return {
        "device_id": device.id,
        "hostname": device.hostname,
        "role": device.role,
        "health": health,
        "neighbor_count": len(neighbors),
        "full_count": full_count,
        "neighbors": neighbors,
        "snapshot_at": snapshot.created_at.isoformat() if snapshot else None,
    }


def build_summary() -> dict:
    devices = device_service.list_devices()
    ospf_devices = [device for device in devices if device.role in ROUTING_ROLES]

    return {
        "devices": {"by_role": _device_role_rollup(devices)},
        "ospf": [_ospf_entry(device) for device in ospf_devices],
    }
