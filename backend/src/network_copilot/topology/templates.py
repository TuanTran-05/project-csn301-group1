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
        "label": "Router",
        "device_type": "cisco_ios",
        "role": "core",
        "prefix": "R",
        "interface_prefix": "GigabitEthernet0/",
    },
    {
        "key": "switch_l3",
        "label": "Switch L3",
        "device_type": "cisco_ios",
        "role": "distribution",
        "prefix": "DSW",
        "interface_prefix": "GigabitEthernet0/",
    },
    {
        "key": "switch",
        "label": "Switch L2",
        "device_type": "cisco_ios",
        "role": "access",
        "prefix": "SW",
        "interface_prefix": "GigabitEthernet0/",
    },
    {
        "key": "firewall",
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


def next_management_ip(project) -> str:
    """Lowest free host address of the project's management network."""
    network = ipaddress.ip_network(project.management_network, strict=False)
    used = {
        row[0]
        for row in db.session.query(Device.management_ip).filter(
            Device.project_id == project.id
        )
    }
    hosts = list(network.hosts())
    ordered = hosts[FIRST_SUGGESTED_HOST - 1 :] + hosts[: FIRST_SUGGESTED_HOST - 1]
    for address in ordered:
        if str(address) not in used:
            return str(address)
    raise ConflictError(
        f"The management network {network} has no free address left."
    )


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
    used = set()
    for link in db.session.query(TopologyLink).filter(
        TopologyLink.project_id == project_id
    ):
        if link.device_a_id == device.id:
            used.add(link.interface_a.lower())
        if link.device_b_id == device.id:
            used.add(link.interface_b.lower())
    number = 0
    while f"{prefix}{number}".lower() in used:
        number += 1
    return f"{prefix}{number}"
