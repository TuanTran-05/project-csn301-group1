"""Automatic addressing: free point-to-point links and LAN subnets.

Subnets come from the project's ``addressing_pool`` (default 10.100.0.0/16),
skipping every range the management network, links and SVIs already use.
"""

import ipaddress

from ..errors import ConflictError
from ..extensions import db
from .model import DesignSvi, TopologyLink

DEFAULT_POOL = "10.100.0.0/16"


def pool_for(project) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(project.addressing_pool or DEFAULT_POOL, strict=False)


def used_networks(project) -> list[ipaddress.IPv4Network]:
    taken = [ipaddress.ip_network(project.management_network, strict=False)]
    for link in db.session.query(TopologyLink).filter(
        TopologyLink.project_id == project.id, TopologyLink.network.isnot(None)
    ):
        taken.append(ipaddress.ip_network(link.network, strict=False))
    for svi in db.session.query(DesignSvi).filter_by(project_id=project.id):
        taken.append(ipaddress.ip_network(f"{svi.ip}/{svi.prefix_length}", strict=False))
    return taken


def allocate(project, prefix: int, reserved=()) -> ipaddress.IPv4Network:
    """Lowest free subnet of the given size in the pool."""
    pool = pool_for(project)
    if prefix < pool.prefixlen:
        raise ConflictError(f"A /{prefix} does not fit in the addressing pool {pool}.")
    taken = used_networks(project) + list(reserved)
    for candidate in pool.subnets(new_prefix=prefix):
        if not any(candidate.overlaps(other) for other in taken):
            return candidate
    raise ConflictError(f"The addressing pool {pool} has no free /{prefix} left.")


def next_link_network(project, reserved=()) -> str:
    return str(allocate(project, 30, reserved))


def next_lan(project, prefix: int = 24, reserved=()) -> ipaddress.IPv4Network:
    return allocate(project, prefix, reserved)


def summary(project) -> dict:
    pool = pool_for(project)
    taken = sorted(
        (n for n in used_networks(project) if n.overlaps(pool)), key=lambda n: (n.network_address, n.prefixlen)
    )
    used = sum(n.num_addresses for n in taken)
    return {
        "pool": str(pool),
        "custom": bool(project.addressing_pool),
        "used": [str(n) for n in taken],
        "utilisation": round(min(1.0, used / pool.num_addresses), 4),
        "next_link": next_link_network(project),
        "next_lan": str(next_lan(project)),
    }
