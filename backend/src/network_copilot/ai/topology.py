"""Network topology derived from monitoring snapshots.

Gives the model enough of the real network to reason about policy - which
VLAN carries which subnet, where its gateway lives, and which interface a
router uses to reach it - without ever handing over a management IP.

Everything here is read from data the monitoring scheduler already
collects and parses; nothing opens an SSH session, so building the map
costs a chat turn nothing.
"""

import ipaddress
import re

from ..devices import service as device_service
from ..extensions import db
from ..monitoring.service import latest_snapshot

INTERFACE_COMMAND = "show ip interface brief"
VLAN_COMMAND = "show vlan brief"
ROUTE_COMMAND = "show ip route"

# Only these roles hold a routing table worth showing the model.
ROUTING_ROLES = {"core", "distribution"}

# Change requests whose interface addresses describe the network as it is
# or is about to be. Failed and cancelled ones never reached the device.
_ADDRESSING_CHANGE_STATUSES = ("success", "running", "approved", "pending_approval")

_INTERFACE_LINE = re.compile(r"^interface\s+(?P<name>\S+)\s*$", re.I)
_ADDRESS_LINE = re.compile(
    r"^ip\s+address\s+(?P<ip>\d+\.\d+\.\d+\.\d+)\s+(?P<mask>\d+\.\d+\.\d+\.\d+)\s*$",
    re.I,
)

# Global-configuration commands that end an "interface" block.
_TOP_LEVEL = ("router ", "ip route", "hostname", "vlan ", "line ", "access-list",
              "ip access-list", "ip dhcp", "ip nat", "route ", "object")

# A switched virtual interface: "Vlan60" carries VLAN 60.
_SVI = re.compile(r"^Vlan(?P<vlan_id>\d+)$", re.I)

# Cisco's protocol letter for a directly connected network. "L" is the
# local /32 host route and is deliberately not accepted: it would make a
# /24 look one address wide and defeat the management filter below.
_CONNECTED = "C"


def _parsed(snapshot, command: str) -> list[dict]:
    """Rows for one command in a snapshot, tolerating anything malformed."""
    if snapshot is None or not isinstance(snapshot.parsed_data, dict):
        return []
    rows = snapshot.parsed_data.get(command)
    return rows if isinstance(rows, list) else []


def _management_ips(devices) -> list:
    addresses = []
    for device in devices:
        if not device.management_ip:
            continue
        try:
            addresses.append(ipaddress.ip_address(device.management_ip))
        except ValueError:
            continue
    return addresses


def _hides_a_management_ip(subnet: str, management_ips) -> bool:
    """True when this subnet must not be shown to the model.

    Fails closed: a subnet that cannot be parsed is treated as unsafe
    rather than waved through, so a malformed value can never become a
    hole in the filter.
    """
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return True
    return any(address in network for address in management_ips)


def _networks_for(device, snapshot) -> list[dict]:
    vlan_names = {
        row["vlan_id"]: row.get("name")
        for row in _parsed(snapshot, VLAN_COMMAND)
        if isinstance(row.get("vlan_id"), int)
    }
    connected = {
        row["interface"]: row["network"]
        for row in _parsed(snapshot, ROUTE_COMMAND)
        if row.get("protocol") == _CONNECTED and row.get("interface")
    }

    entries = []
    for row in _parsed(snapshot, INTERFACE_COMMAND):
        name = row.get("interface") or ""
        match = _SVI.match(name)
        if match is None:
            continue

        address = row.get("ip_address")
        if not address or address == "unassigned":
            continue

        subnet = connected.get(name)
        if subnet is None:
            # No connected route means no prefix length. A half-populated
            # entry would misstate the size of the network.
            continue

        vlan_id = int(match.group("vlan_id"))
        entry = {
            "vlan_id": vlan_id,
            "subnet": subnet,
            "gateway": address,
            "gateway_device": device.hostname,
            "gateway_interface": name,
        }
        vlan_name = vlan_names.get(vlan_id)
        if vlan_name:
            entry["name"] = vlan_name
        entries.append(entry)
    return entries


def _routing_for(devices, snapshots, networks) -> list[dict]:
    """How each router reaches the networks in the map.

    Restricted to networks already in "networks", which means the
    management filter applied there covers this too: a route to a filtered
    network cannot reappear through this door. Transit links and the
    default route are dropped as noise.
    """
    known = {entry["subnet"] for entry in networks}

    routing = []
    for device in devices:
        if device.role not in ROUTING_ROLES:
            continue
        routes = [
            {
                "network": row["network"],
                "interface": row.get("interface"),
                "protocol": row.get("protocol"),
            }
            for row in _parsed(snapshots[device.id], ROUTE_COMMAND)
            if row.get("network") in known
        ]
        if routes:
            routing.append({"device": device.hostname, "routes": routes})
    return routing


def build_topology(project_id: int | None = None) -> dict:
    """The network as the model is allowed to see it.

    Built from one project's devices when ``project_id`` is given, so another
    project's subnets, VLANs and routes never reach the model.
    """
    devices = device_service.list_devices(project_id=project_id, deployable=True)
    snapshots = {device.id: latest_snapshot(device.id) for device in devices}
    management_ips = _management_ips(devices)

    networks: list[dict] = []
    for device in devices:
        for entry in _networks_for(device, snapshots[device.id]):
            if _hides_a_management_ip(entry["subnet"], management_ips):
                continue
            networks.append(entry)

    networks.sort(key=lambda entry: (entry["vlan_id"], entry["gateway_device"]))

    return {
        "networks": networks,
        "routing": _routing_for(devices, snapshots, networks),
    }


def _interface_entry(hostname, interface, address, source, management_ips):
    """One addressed interface, or None when it is malformed or management."""
    try:
        network = ipaddress.ip_interface(address).network
    except ValueError:
        return None
    if _hides_a_management_ip(str(network), management_ips):
        return None
    return {
        "device": hostname,
        "interface": interface,
        "address": address,
        "network": str(network),
        "source": source,
    }


def _addresses_from_commands(commands) -> list[tuple[str, str]]:
    """(interface, "ip/prefix") pairs set by an IOS configuration batch."""
    found = []
    interface = None
    for line in commands or []:
        if not isinstance(line, str):
            continue
        text = line.strip()
        match = _INTERFACE_LINE.match(text)
        if match:
            interface = match.group("name")
            continue
        match = _ADDRESS_LINE.match(text)
        if match and interface:
            try:
                prefix = ipaddress.ip_network(
                    f"0.0.0.0/{match.group('mask')}"
                ).prefixlen
            except ValueError:
                continue
            found.append((interface, f"{match.group('ip')}/{prefix}"))
        elif text.casefold() in ("exit", "end") or text.casefold().startswith(_TOP_LEVEL):
            interface = None
    return found


def build_addressing(project_id: int | None) -> dict:
    """Interface addresses, routed links and static routes the project defines.

    Lets the model work out next hops and destination networks for routing
    requests from what is already saved - the topology diagram, the network
    design and configuration batches - instead of asking the user. As in
    build_topology, any network holding a management IP is dropped.
    """
    if project_id is None:
        return {"routed_links": [], "interfaces": [], "static_routes": []}

    from ..changes.model import ChangeRequest
    from ..devices.model import Device
    from ..topology.model import (
        DesignLoopback,
        DesignStaticRoute,
        DesignSubif,
        DesignSvi,
        TopologyLink,
    )

    devices = (
        db.session.query(Device).filter(Device.project_id == project_id).all()
    )
    management_ips = _management_ips(devices)
    hostnames = {device.id: device.hostname for device in devices}

    routed_links = []
    interfaces: dict[tuple[str, str], dict] = {}

    def remember(entry):
        if entry is not None:
            interfaces[(entry["device"], entry["interface"].casefold())] = entry

    links = (
        db.session.query(TopologyLink)
        .filter(TopologyLink.project_id == project_id)
        .order_by(TopologyLink.id)
    )
    for link in links:
        if link.link_type != "routed" or not link.network:
            continue
        if _hides_a_management_ip(link.network, management_ips):
            continue
        prefix = ipaddress.ip_network(link.network, strict=False).prefixlen
        a = hostnames.get(link.device_a_id)
        b = hostnames.get(link.device_b_id)
        routed_links.append({
            "network": link.network,
            "device_a": a, "interface_a": link.interface_a, "ip_a": link.ip_a,
            "device_b": b, "interface_b": link.interface_b, "ip_b": link.ip_b,
        })
        for host, iface, ip in ((a, link.interface_a, link.ip_a), (b, link.interface_b, link.ip_b)):
            if host and ip:
                remember(_interface_entry(host, iface, f"{ip}/{prefix}", "diagram", management_ips))

    for svi in db.session.query(DesignSvi).filter(DesignSvi.project_id == project_id):
        remember(_interface_entry(
            hostnames.get(svi.device_id), f"Vlan{svi.vlan_id}",
            f"{svi.ip}/{svi.prefix_length}", "design", management_ips,
        ))
    for sub in db.session.query(DesignSubif).filter(DesignSubif.project_id == project_id):
        remember(_interface_entry(
            hostnames.get(sub.device_id), f"{sub.interface}.{sub.vlan_id}",
            f"{sub.ip}/{sub.prefix_length}", "design", management_ips,
        ))
    for loop in db.session.query(DesignLoopback).filter(DesignLoopback.project_id == project_id):
        remember(_interface_entry(
            hostnames.get(loop.device_id), f"Loopback{loop.number}",
            f"{loop.ip}/32", "design", management_ips,
        ))

    # Batches last, oldest first, so the newest configuration wins.
    changes = (
        db.session.query(ChangeRequest)
        .join(Device, ChangeRequest.device_id == Device.id)
        .filter(
            Device.project_id == project_id,
            ChangeRequest.execution_mode == "config",
            ChangeRequest.status.in_(_ADDRESSING_CHANGE_STATUSES),
        )
        .order_by(ChangeRequest.id)
    )
    for change in changes:
        source = "applied_batch" if change.status == "success" else "pending_batch"
        for iface, address in _addresses_from_commands(change.commands):
            remember(_interface_entry(
                change.target_hostname, iface, address, source, management_ips,
            ))

    static_routes = []
    for route in db.session.query(DesignStaticRoute).filter(
        DesignStaticRoute.project_id == project_id
    ):
        destination = f"{route.network}/{route.prefix_length}"
        # A default route contains every address; only a specific route can
        # reveal a management network.
        if route.prefix_length and _hides_a_management_ip(destination, management_ips):
            continue
        if _hides_a_management_ip(f"{route.next_hop}/32", management_ips):
            continue
        static_routes.append({
            "device": hostnames.get(route.device_id),
            "network": destination,
            "next_hop": route.next_hop,
        })

    return {
        "routed_links": routed_links,
        "interfaces": sorted(
            (entry for entry in interfaces.values() if entry["device"]),
            key=lambda entry: (entry["device"], entry["interface"]),
        ),
        "static_routes": static_routes,
    }
