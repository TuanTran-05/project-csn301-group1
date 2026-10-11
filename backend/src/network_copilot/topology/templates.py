"""Device templates for the topology palette (drag a device onto the canvas)."""

import ipaddress

from ..devices.model import Device
from ..errors import ConflictError, NotFoundError
from ..extensions import db

# What a freshly dropped device looks like. The operator can edit everything
# afterwards; the template only saves typing.
DEVICE_TEMPLATES = (
    {
        "key": "router",
        "ports": 8,
        "label": "Router",
        "device_type": "cisco_ios",
        "role": "core",
        "prefix": "R",
        "interface_prefix": "GigabitEthernet0/",
    },
    {
        "key": "switch_l3",
        "ports": 24,
        "label": "Switch L3",
        "device_type": "cisco_ios",
        "role": "distribution",
        "prefix": "DSW",
        "interface_prefix": "GigabitEthernet0/",
    },
    {
        "key": "switch",
        "ports": 24,
        "label": "Switch L2",
        "device_type": "cisco_ios",
        "role": "access",
        "prefix": "SW",
        "interface_prefix": "GigabitEthernet0/",
    },
    {
        "key": "firewall",
        "ports": 8,
        "label": "Firewall ASA",
        "device_type": "cisco_asa",
        "role": "firewall",
        "prefix": "FW",
        "interface_prefix": "GigabitEthernet0/",
    },
)

# Management addresses are suggested from the 10th host upward, leaving the
# low addresses for the gateway and infrastructure.
FIRST_SUGGESTED_HOST = 10


def template_for_device(device) -> dict:
    """The template a device most resembles (decides its port inventory)."""
    if device.device_type == "cisco_asa" or device.role == "firewall":
        return get_template("firewall")
    if device.role == "core":
        return get_template("router")
    if device.role == "distribution":
        return get_template("switch_l3")
    return get_template("switch")


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
    for number in range(template["ports"]):
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

    template = next(
        (
            t
            for t in DEVICE_TEMPLATES
            if t["device_type"] == device.device_type
        ),
        DEVICE_TEMPLATES[0],
    )
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
