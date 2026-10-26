"""Device templates for the topology palette (drag a device onto the canvas)."""

import ipaddress

from ..devices.model import Device
from ..errors import ConflictError, NotFoundError
from ..extensions import db

# What a freshly dropped device looks like. The operator can edit everything
# afterwards; the template only saves typing.
def _managed(key, label, kind, device_type, role, prefix, ports):
    return {
        "key": key, "label": label, "kind": kind, "managed": True, "ports": ports,
        "device_type": device_type, "role": role, "prefix": prefix,
        "interface_prefix": "GigabitEthernet0/",
    }


def _unmanaged(key, label, role, prefix, ports, interface_prefix="Ethernet0/"):
    return {
        "key": key, "label": label, "kind": key, "managed": False, "ports": ports,
        "device_type": "generic", "role": role, "prefix": prefix,
        "interface_prefix": interface_prefix,
    }


DEVICE_TEMPLATES = (
    _managed("router", "Router", "router", "cisco_ios", "core", "R", 8),
    _managed("switch_l3", "Switch L3", "l3_switch", "cisco_ios", "distribution", "DSW", 24),
    _managed("switch", "Switch L2", "switch", "cisco_ios", "access", "SW", 24),
    _managed("firewall", "Firewall ASA", "firewall", "cisco_asa", "firewall", "FW", 8),
    # PAN-OS data ports are ethernet1/1, ethernet1/2, ... (numbering starts at 1).
    {**_managed("firewall_pa", "Firewall Palo Alto", "firewall", "paloalto_panos", "firewall", "PA", 8),
     "interface_prefix": "ethernet1/", "first_port": 1},
    _unmanaged("pc", "PC / Laptop", "host", "PC", 1),
    _unmanaged("server", "Server", "server", "SRV", 2),
    _unmanaged("cloud", "Cloud / Internet", "internet", "CLOUD", 8),
    _unmanaged("ap", "Access Point", "wireless", "AP", 2),
    _unmanaged("segment", "Shared segment", "segment", "SEG", 48, "Port"),
)

# Management addresses are suggested from the 10th host upward, leaving the
# low addresses for the gateway and infrastructure.
FIRST_SUGGESTED_HOST = 10


def template_for_device(device) -> dict:
    """The template a device most resembles (decides its port inventory)."""
    kind = getattr(device, "kind", None)
    if getattr(device, "device_type", None) == "paloalto_panos":
        return get_template("firewall_pa")
    if kind in _BY_KIND:
        return _BY_KIND[kind]
    if device.device_type == "cisco_asa" or device.role == "firewall":
        return get_template("firewall")
    if device.role == "core":
        return get_template("router")
    if device.role == "distribution":
        return get_template("switch_l3")
    return get_template("switch")


# The first template of each kind is its default (Firewall ASA for "firewall").
_BY_KIND: dict = {}
for _template in DEVICE_TEMPLATES:
    _BY_KIND.setdefault(_template["kind"], _template)


def port_inventory(project_id: int, device) -> list[dict]:
    """The device's ports, each marked free or used (by a link or access port)."""
    from .interfaces import normalize_label
    from .model import DesignAccessPort, TopologyLink

    template = template_for_device(device)
    used: dict[str, str] = {}

    def canon(name):
        return (normalize_label(name) or name).lower()

    for link in db.session.query(TopologyLink).filter(TopologyLink.project_id == project_id):
        if link.device_a_id == device.id:
            peer = link.device_b.hostname if link.device_b else "?"
            used[canon(link.interface_a)] = f"link to {peer}"
        if link.device_b_id == device.id:
            peer = link.device_a.hostname if link.device_a else "?"
            used[canon(link.interface_b)] = f"link to {peer}"
    for port in db.session.query(DesignAccessPort).filter_by(device_id=device.id):
        used[canon(port.interface)] = f"access VLAN {port.vlan_id}"

    ports = []
    first = template.get("first_port", 0)
    for number in range(first, first + template["ports"]):
        name = f"{template['interface_prefix']}{number}"
        ports.append({"name": name, "used": canon(name) in used, "used_by": used.get(canon(name))})
    return ports


def get_template(key: str) -> dict:
    for template in DEVICE_TEMPLATES:
        if template["key"] == key:
            return template
    raise NotFoundError(f"Unknown device template {key!r}.")


def next_hostname(project_id: int, prefix: str) -> str:
    """Smallest unused ``<prefix><n>``: R1, R2, ... (reuses gaps)."""
    taken = {
        row[0].upper()
        for row in db.session.query(Device.hostname).filter(
            Device.project_id == project_id, Device.hostname.like(f"{prefix}%")
        )
    }
    number = 1
    while f"{prefix}{number}".upper() in taken:
        number += 1
    return f"{prefix}{number}"


def free_management_ips(project, count: int) -> list[str]:
    """The ``count`` lowest free host addresses of the management network."""
    network = ipaddress.ip_network(project.management_network, strict=False)
    used = {
        row[0]
        for row in db.session.query(Device.management_ip).filter(
            Device.project_id == project.id
        )
    }
    hosts = list(network.hosts())
    ordered = hosts[FIRST_SUGGESTED_HOST - 1 :] + hosts[: FIRST_SUGGESTED_HOST - 1]
    free = [str(address) for address in ordered if str(address) not in used]
    if len(free) < count:
        raise ConflictError(
            f"The management network {network} has no free address left."
        )
    return free[:count]


def next_management_ip(project) -> str:
    """Lowest free host address of the project's management network."""
    return free_management_ips(project, 1)[0]


def next_free_interface(project_id: int, device: Device) -> str:
    """Lowest unused ``GigabitEthernet0/<n>`` on a device."""
    from .model import TopologyLink

    template = template_for_device(device)
    prefix = template["interface_prefix"]
    from .interfaces import normalize_label
    from .model import DesignAccessPort

    def canon(name):
        return (normalize_label(name) or name).lower()

    used = set()
    for link in db.session.query(TopologyLink).filter(
        TopologyLink.project_id == project_id
    ):
        if link.device_a_id == device.id:
            used.add(canon(link.interface_a))
        if link.device_b_id == device.id:
            used.add(canon(link.interface_b))
    for port in db.session.query(DesignAccessPort).filter_by(device_id=device.id):
        used.add(canon(port.interface))
    number = 0
    while f"{prefix}{number}".lower() in used:
        number += 1
    return f"{prefix}{number}"
