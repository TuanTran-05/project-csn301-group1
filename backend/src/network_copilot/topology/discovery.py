"""Discover the real cabling with CDP / LLDP and compare it with the design.

Read-only: it runs two show commands per device and never changes a device or
the design (applying discovered links is a separate, explicit call).
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from ..devices import service as device_service
from ..errors import AppError, ValidationError
from ..parsers.neighbors import parse_cdp_detail, parse_lldp_detail
from ..ssh.client import build_client_for_device
from . import service
from .interfaces import normalize_label

logger = logging.getLogger(__name__)

CDP_COMMAND = "show cdp neighbors detail"
LLDP_COMMAND = "show lldp neighbors detail"
MAX_WORKERS = 4


def _short_name(name: str) -> str:
    return name.split(".")[0].upper()


def _poll(device, client) -> dict:
    """Neighbors of one device: CDP first, LLDP when CDP says nothing."""
    try:
        neighbors = parse_cdp_detail(client.run_show(CDP_COMMAND).output)
        if not neighbors:
            neighbors = parse_lldp_detail(client.run_show(LLDP_COMMAND).output)
        return {"device": device.hostname, "neighbors": neighbors}
    except AppError as exc:
        return {"device": device.hostname, "error": exc.message}
    except Exception:  # pragma: no cover - a poll must not break discovery
        logger.exception("Discovery failed on %s", device.hostname)
        return {"device": device.hostname, "error": "Unexpected error."}


def _endpoint_key(hostname: str, interface: str) -> tuple[str, str]:
    return hostname.upper(), (normalize_label(interface) or interface).lower()


def discover(project) -> dict:
    devices = [
        d for d in device_service.list_devices(project_id=project.id)
        if d.device_type == "cisco_ios"
    ]
    if not devices:
        raise ValidationError("The project has no Cisco IOS devices to ask.")

    # Clients are built here (they read stored credentials from the database);
    # only the network round trips run in threads.
    prepared, errors = [], []
    for device in devices:
        try:
            prepared.append((device, build_client_for_device(device)))
        except AppError as exc:
            errors.append({"device": device.hostname, "error": exc.message})

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        polled = list(pool.map(lambda pair: _poll(*pair), prepared))
    errors.extend({"device": p["device"], "error": p["error"]} for p in polled if "error" in p)
    answered = {p["device"].upper() for p in polled if "error" not in p}

    known = {d.hostname.upper(): d for d in device_service.list_devices(project_id=project.id)}
    found: dict[frozenset, dict] = {}
    unmatched = []
    for entry in polled:
        for n in entry.get("neighbors", []):
            peer = known.get(_short_name(n["neighbor"]))
            if peer is None:
                unmatched.append({"device": entry["device"], "neighbor": n["neighbor"],
                                  "interface": n["local_interface"], "ip": n["ip"]})
                continue
            a = _endpoint_key(entry["device"], n["local_interface"])
            b = _endpoint_key(peer.hostname, n["remote_interface"])
            if a[0] == b[0]:
                continue
            key = frozenset((a, b))
            link = found.setdefault(key, {
                "hostname_a": entry["device"], "interface_a": normalize_label(n["local_interface"]) or n["local_interface"],
                "hostname_b": peer.hostname, "interface_b": normalize_label(n["remote_interface"]) or n["remote_interface"],
                "device_a_id": known[entry["device"].upper()].id, "device_b_id": peer.id,
                "protocol": n["protocol"], "seen_from": [],
            })
            link["seen_from"].append(entry["device"])

    return {**_compare(project, found, answered), "discovered": list(found.values()),
            "unmatched": unmatched, "errors": errors, "polled": sorted(answered)}


def _compare(project, found: dict, answered: set) -> dict:
    designed = service.list_links(project.id)
    matched, missing, unknown = [], [], []
    designed_keys = set()
    for link in designed:
        a = _endpoint_key(link.device_a.hostname, link.interface_a)
        b = _endpoint_key(link.device_b.hostname, link.interface_b)
        key = frozenset((a, b))
        designed_keys.add(key)
        if key in found:
            matched.append(link.id)
        elif a[0] in answered or b[0] in answered:
            # One end answered and did not list the cable: it is not there.
            missing.append(link.id)
        else:
            unknown.append(link.id)
    unplanned = [
        {k: v for k, v in link.items() if k != "seen_from"}
        for key, link in found.items() if key not in designed_keys
    ]
    return {"matched": matched, "missing": missing, "unknown": unknown, "unplanned": unplanned}


def apply_discovered(project, links: list[dict]) -> dict:
    """Add chosen discovered cables to the design (physical links)."""
    created, skipped = [], []
    for item in links:
        try:
            link = service.create_link(project, {
                "device_a_id": item.get("device_a_id"), "interface_a": item.get("interface_a"),
                "device_b_id": item.get("device_b_id"), "interface_b": item.get("interface_b"),
            })
            created.append(link.to_dict())
        except AppError as exc:
            service.db.session.rollback()
            skipped.append({"item": item, "message": exc.message})
    return {"created": created, "skipped": skipped}


