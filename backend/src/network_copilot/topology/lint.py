"""Design checks: problems worth fixing before generating configuration."""

import ipaddress
from collections import defaultdict

from ..devices.model import Device
from ..extensions import db
from . import service
from .model import DesignAccessPort, DesignOspf, DesignStaticRoute, DesignVlan


def _issue(level: str, code: str, message: str, device=None, link=None) -> dict:
    return {
        "level": level,
        "code": code,
        "message": message,
        "device_id": device.id if device is not None else None,
        "link_id": link.id if link is not None else None,
    }


def lint(project) -> dict:
    devices = db.session.query(Device).filter(Device.project_id == project.id).all()
    links = service.list_links(project.id)
    issues = []

    degree = defaultdict(int)
    routed_by = defaultdict(list)
    trunks_by = defaultdict(list)
    for link in links:
        degree[link.device_a_id] += 1
        degree[link.device_b_id] += 1
        if link.link_type == "routed":
            routed_by[link.device_a_id].append(link)
            routed_by[link.device_b_id].append(link)
        elif link.link_type == "trunk":
            trunks_by[link.device_a_id].append(link)
            trunks_by[link.device_b_id].append(link)

    channels = defaultdict(list)
    for link in links:
        if link.channel_id:
            channels[(frozenset((link.device_a_id, link.device_b_id)), link.channel_id)].append(link)
    for (pair, number), members in channels.items():
        if len(members) == 1:
            issues.append(_issue(
                "info", "channel_single",
                f"Port-channel {number} has only one member link; add a parallel cable to bundle.",
                link=members[0],
            ))

    ospf = {row.device_id for row in db.session.query(DesignOspf).filter_by(project_id=project.id)}
    statics = {row.device_id for row in db.session.query(DesignStaticRoute).filter_by(project_id=project.id)}
    vlans = {row.vlan_id for row in db.session.query(DesignVlan).filter_by(project_id=project.id)}
    access = defaultdict(set)
    for row in db.session.query(DesignAccessPort).filter_by(project_id=project.id):
        access[row.device_id].add(row.vlan_id)

    data_ips = defaultdict(list)
    for device in devices:
        if degree[device.id] == 0 and len(devices) > 1:
            issues.append(_issue("warning", "isolated", f"{device.hostname} is not connected to anything.", device))
        if device.lifecycle == "planned":
            issues.append(_issue("info", "planned", f"{device.hostname} is only planned: no management IP yet.", device))
        if len(routed_by[device.id]) >= 2 and device.id not in ospf and device.id not in statics:
            issues.append(_issue(
                "warning", "no_routing",
                f"{device.hostname} joins {len(routed_by[device.id])} routed links but has no OSPF or static route.",
                device,
            ))
        if device.kind == "firewall":
            bare = [l for l in links if device.id in (l.device_a_id, l.device_b_id)
                    and not (l.nameif_a if l.device_a_id == device.id else l.nameif_b)]
            if bare:
                issues.append(_issue(
                    "warning", "no_nameif",
                    f"{device.hostname} has {len(bare)} link(s) without a nameif/security level.", device,
                ))
        missing = access[device.id] - vlans
        if missing:
            issues.append(_issue("error", "undefined_vlan",
                                 f"{device.hostname} uses undefined VLAN(s) {sorted(missing)}.", device))
        if access[device.id] and trunks_by[device.id]:
            for link in trunks_by[device.id]:
                allowed = _allowed(link.allowed_vlans)
                dropped = sorted(v for v in access[device.id] if allowed is not None and v not in allowed)
                if dropped:
                    issues.append(_issue(
                        "warning", "trunk_drops_vlan",
                        f"Trunk {link.id} does not carry VLAN(s) {dropped} used on {device.hostname}.",
                        device, link,
                    ))
        if device.data_ip:
            data_ips[device.data_ip].append(device)
            if device.data_gateway:
                try:
                    same = ipaddress.ip_address(device.data_gateway) == ipaddress.ip_address(device.data_ip)
                except ValueError:
                    same = False
                if same:
                    issues.append(_issue("error", "gateway_is_self",
                                         f"{device.hostname} uses its own address as gateway.", device))
    for address, owners in data_ips.items():
        if len(owners) > 1:
            names = ", ".join(sorted(d.hostname for d in owners))
            issues.append(_issue("error", "duplicate_ip", f"{address} is assigned to {names}.", owners[0]))

    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["level"]], i["code"]))
    counts = {level: sum(1 for i in issues if i["level"] == level) for level in order}
    return {"issues": issues, "counts": counts, "ok": counts["error"] == 0}


def _allowed(value: str | None):
    """VLAN ids carried by a trunk; None means all."""
    if not value:
        return None
    result = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            low, high = part.split("-", 1)
            result.update(range(int(low), int(high) + 1))
        elif part:
            result.add(int(part))
    return result
