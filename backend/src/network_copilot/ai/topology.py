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
from ..monitoring.service import latest_snapshot

INTERFACE_COMMAND = "show ip interface brief"
VLAN_COMMAND = "show vlan brief"
ROUTE_COMMAND = "show ip route"

# Only these roles hold a routing table worth showing the model.
ROUTING_ROLES = {"core", "distribution"}

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


def build_topology() -> dict:
    """The network as the model is allowed to see it."""
    devices = device_service.list_devices()
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
