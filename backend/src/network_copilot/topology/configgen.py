"""Turn a project's topology design into per-device configuration.

Nothing here talks to a device or writes the database. The result is a plan
(commands per device); ``create_design_preview`` hands the plan to the normal
change workflow, so every generated change still goes through
Preview -> Approve -> Apply -> Verify like any other.

Per Cisco IOS device the plan covers, in order: VLANs (switches), access ports,
cabled interfaces (physical / routed / trunk links), SVIs, static routes and
single-area OSPF. Per ASA: routed interfaces (nameif, security level, address)
and static routes. Anything the design asks for that a device type cannot take
is reported in that device's ``notes`` instead of being guessed.
"""

import ipaddress
import re

from ..changes import batch_service
from ..changes.batch_service import BatchOperation
from ..errors import ValidationError
from ..extensions import db
from .design import SWITCH_ROLES
from .model import (
    DesignAccessPort,
    DesignOspf,
    DesignStaticRoute,
    DesignSvi,
    DesignVlan,
)
from .service import list_links


def _label(link, peer, peer_interface) -> str:
    return link.description or f"LINK TO {peer.hostname} {peer_interface}"


def _ends(link, side: str):
    if side == "a":
        return (link.device_a, link.interface_a, link.device_b, link.interface_b,
                link.ip_a, link.nameif_a, link.security_a)
    return (link.device_b, link.interface_b, link.device_a, link.interface_a,
            link.ip_b, link.nameif_b, link.security_b)


def _ios_link_commands(link, side: str) -> list[str]:
    _device, interface, peer, peer_interface, address, *_ = _ends(link, side)
    commands = [f"interface {interface}", f"description {_label(link, peer, peer_interface)}"]
    if link.link_type == "routed":
        network = ipaddress.ip_network(link.network)
        commands.append(f"ip address {address} {network.netmask}")
    elif link.link_type == "trunk":
        commands.append("switchport mode trunk")
        if link.allowed_vlans:
            commands.append(f"switchport trunk allowed vlan {link.allowed_vlans}")
    if link.bring_up:
        commands.append("no shutdown")
    return commands


def asa_nameif(link, side: str) -> str:
    """The interface name, given or derived from the neighbour ("to-r1")."""
    _device, _interface, peer, _pi, _addr, nameif, _security = _ends(link, side)
    if nameif:
        return nameif
    return "to-" + re.sub(r"[^a-z0-9_-]", "-", peer.hostname.lower())


def _asa_link_commands(link, side: str) -> list[str]:
    _device, interface, _peer, _pi, address, _nameif, security = _ends(link, side)
    network = ipaddress.ip_network(link.network)
    commands = [
        f"interface {interface}",
        f"nameif {asa_nameif(link, side)}",
        f"security-level {0 if security is None else security}",
        f"ip address {address} {network.netmask}",
    ]
    if link.bring_up:
        commands.append("no shutdown")
    return commands


def _ospf_networks(device, links, svis) -> list[ipaddress.IPv4Network]:
    networks = set()
    for link in links:
        if link.network and device.id in (link.device_a_id, link.device_b_id):
            networks.add(ipaddress.ip_network(link.network))
    for svi in svis:
        if svi.device_id == device.id:
            networks.add(ipaddress.ip_network(f"{svi.ip}/{svi.prefix_length}", strict=False))
    return sorted(networks, key=lambda n: (int(n.network_address), n.prefixlen))


def build_plan(project, link_ids: list[int] | None = None) -> dict:
    """Commands per device for the chosen links (all links and design by default).

    Choosing links limits the plan to those cables: VLANs, access ports, SVIs,
    routes and OSPF belong to the whole design and are only included when no
    selection is made.
    """
    all_links = list_links(project.id)
    links = all_links
    if link_ids is not None:
        wanted = set(link_ids)
        unknown = wanted - {link.id for link in all_links}
        if unknown:
            raise ValidationError("Unknown link ids.", {"link_ids": sorted(unknown)})
        links = [link for link in all_links if link.id in wanted]
    whole_design = link_ids is None

    def rows(model):
        return db.session.query(model).filter_by(project_id=project.id).order_by(model.id).all()

    vlans = rows(DesignVlan) if whole_design else []
    ports = rows(DesignAccessPort) if whole_design else []
    svis = rows(DesignSvi) if whole_design else []
    routes = rows(DesignStaticRoute) if whole_design else []
    ospfs = rows(DesignOspf) if whole_design else []
    if not (links or vlans or ports or svis or routes or ospfs):
        raise ValidationError("The design has no links or settings to configure.")

    entries: dict[int, dict] = {}

    def entry(device):
        return entries.setdefault(device.id, {
            "device": device, "vlans": [], "ports": [], "links": [], "svis": [],
            "routes": [], "ospf": [], "link_ids": [], "notes": [],
        })

    for link in links:
        for side in ("a", "b"):
            device, *_rest = _ends(link, side)
            item = entry(device)
            item["link_ids"].append(link.id)
            if device.device_type == "cisco_ios":
                item["links"].extend(_ios_link_commands(link, side))
            elif link.link_type == "routed":
                item["links"].extend(_asa_link_commands(link, side))
            else:
                item["notes"].append(
                    f"{_ends(link, side)[1]}: only routed links are generated for ASA "
                    f"(this one is {link.link_type})."
                )

    if vlans:
        # The VLAN database exists on every IOS switch of the project.
        for device in _project_devices(project):
            if device.device_type != "cisco_ios" or device.role not in SWITCH_ROLES:
                continue
            item = entry(device)
            for vlan in sorted(vlans, key=lambda v: v.vlan_id):
                item["vlans"].extend([f"vlan {vlan.vlan_id}", f"name {vlan.name}"])
    for port in ports:
        entry(port.device)["ports"].extend([
            f"interface {port.interface}", "switchport mode access",
            f"switchport access vlan {port.vlan_id}",
        ])
    for svi in svis:
        network = ipaddress.ip_network(f"{svi.ip}/{svi.prefix_length}", strict=False)
        entry(svi.device)["svis"].extend([
            f"interface Vlan{svi.vlan_id}", f"ip address {svi.ip} {network.netmask}", "no shutdown",
        ])
    for route in routes:
        network = ipaddress.ip_network(f"{route.network}/{route.prefix_length}")
        item = entry(route.device)
        if route.device.device_type == "cisco_ios":
            item["routes"].append(f"ip route {network.network_address} {network.netmask} {route.next_hop}")
            continue
        nameif = _asa_exit(route, links)
        if nameif is None:
            item["notes"].append(
                f"Route to {network} via {route.next_hop}: no routed link reaches that "
                "next hop, so the ASA interface name is unknown."
            )
        else:
            item["routes"].append(
                f"route {nameif} {network.network_address} {network.netmask} {route.next_hop}"
            )
    for ospf in ospfs:
        item = entry(ospf.device)
        networks = _ospf_networks(ospf.device, all_links, svis or rows(DesignSvi))
        if ospf.device.device_type != "cisco_ios":
            item["notes"].append("OSPF is only generated for Cisco IOS devices.")
        elif not networks:
            item["notes"].append("OSPF is enabled but the device has no routed link or SVI to advertise.")
        else:
            commands = [f"router ospf {ospf.process_id}"]
            if ospf.router_id:
                commands.append(f"router-id {ospf.router_id}")
            commands.extend(f"network {n.network_address} {n.hostmask} area 0" for n in networks)
            item["ospf"].extend(commands)

    devices, skipped = [], []
    for item in entries.values():
        device = item["device"]
        commands = (item["vlans"] + item["ports"] + item["links"] + item["svis"]
                    + item["routes"] + item["ospf"])
        if not commands:
            skipped.append({
                "device_id": device.id, "hostname": device.hostname,
                "reason": " ".join(item["notes"]) or
                          f"Configuration generation does not support {device.device_type}.",
            })
            continue
        devices.append({
            "device_id": device.id, "hostname": device.hostname, "commands": commands,
            "link_ids": item["link_ids"], "notes": item["notes"],
        })
    return {
        "devices": sorted(devices, key=lambda d: d["hostname"]),
        "skipped": sorted(skipped, key=lambda s: s["hostname"]),
    }


def _project_devices(project):
    from ..devices.model import Device

    return db.session.query(Device).filter_by(project_id=project.id).all()


def _asa_exit(route, links) -> str | None:
    next_hop = ipaddress.ip_address(route.next_hop)
    for link in links:
        if link.network and next_hop in ipaddress.ip_network(link.network):
            for side in ("a", "b"):
                device, *_rest = _ends(link, side)
                if device.id == route.device_id:
                    return asa_nameif(link, side)
    return None


def create_design_preview(project, user_id: int | None, link_ids=None):
    """Freeze the plan into a change batch awaiting approval."""
    plan = build_plan(project, link_ids)
    if not plan["devices"]:
        raise ValidationError(
            "No device in the selection can be configured automatically.",
            {"skipped": plan["skipped"]},
        )

    operations = [
        BatchOperation(
            device_hostnames=[entry["hostname"]],
            execution_mode="config",
            commands=entry["commands"],
            verification_commands=[],
        )
        for entry in plan["devices"]
    ]
    batch = batch_service.create_batch_preview(
        user_id=user_id,
        operations=operations,
        description=f"Apply network design of project {project.name}"[:255],
        source="design",
        project_id=project.id,
    )
    return batch, plan
