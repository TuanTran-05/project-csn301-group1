"""Ready-made topologies: one call builds devices, cables and positions.

Every blueprint is a function drawing on a small builder. Devices are
created through the normal validation path, so names, ports and addresses
follow the same rules as the palette. A failure removes what was built.
"""

import math

from pydantic import BaseModel, ConfigDict, Field

from ..devices import service as device_service
from ..errors import AppError, NotFoundError
from ..projects.service import validate
from . import service

WIDTH, HEIGHT = 960, 560


class Builder:
    def __init__(self, project, planned: bool):
        self.project = project
        self.planned = planned
        self.devices = []
        self.links = []

    def device(self, template: str, x: float, y: float):
        payload = {"template": template, "x": round(x), "y": round(y)}
        if self.planned:
            payload["planned"] = True
        device = service.quick_add_device(self.project, payload)
        self.devices.append(device)
        return device

    def link(self, a, b, link_type="physical", **extra):
        payload = {"device_a_id": a.id, "device_b_id": b.id, "link_type": link_type, **extra}
        if link_type == "routed":
            payload.setdefault("network", "auto")
        link = service.create_link(self.project, payload)
        self.links.append(link)
        return link

    def row(self, template: str, count: int, y: float, left=60, right=WIDTH - 60):
        step = (right - left) / max(count, 1)
        return [self.device(template, left + step * (i + 0.5), y) for i in range(count)]


# -- blueprints ---------------------------------------------------------------


def star(b: Builder, access: int, hosts: int):
    core = b.device("router", WIDTH / 2, 70)
    switches = b.row("switch", access, 250)
    for switch in switches:
        b.link(core, switch)
    for index, switch in enumerate(switches):
        step = (WIDTH - 120) / max(access, 1)
        left = 60 + step * index
        for pc in b.row("pc", hosts, 440, left, left + step):
            b.link(switch, pc)


def network_star(b: Builder, access: int):
    """An exact-count design for the conversational topology builder."""
    core = b.device("switch_l3", WIDTH / 2, 85)
    switches = b.row("switch", access, 300)
    for switch in switches:
        b.link(core, switch, "trunk")


def branch(b: Builder, hosts: int):
    cloud = b.device("cloud", WIDTH / 2, 50)
    router = b.device("router", WIDTH / 2, 190)
    switch = b.device("switch", WIDTH / 2, 330)
    b.link(cloud, router)
    b.link(router, switch)
    for pc in b.row("pc", hosts, 480):
        b.link(switch, pc)


def campus(b: Builder, access: int, hosts: int):
    cloud = b.device("cloud", WIDTH / 2, 40)
    firewall = b.device("firewall", WIDTH / 2, 130)
    b.link(cloud, firewall)
    dist = [b.device("switch_l3", WIDTH / 2 - 150, 240), b.device("switch_l3", WIDTH / 2 + 150, 240)]
    for switch in dist:
        b.link(firewall, switch, "routed")
    b.link(dist[0], dist[1], "trunk")
    switches = b.row("switch", access, 380)
    for switch in switches:
        for upstream in dist:
            b.link(upstream, switch, "trunk")
    for index, switch in enumerate(switches):
        step = (WIDTH - 120) / max(access, 1)
        left = 60 + step * index
        for pc in b.row("pc", hosts, 500, left, left + step):
            b.link(switch, pc)


def dmz(b: Builder, hosts: int, servers: int):
    cloud = b.device("cloud", WIDTH / 2, 50)
    firewall = b.device("firewall", WIDTH / 2, 190)
    inside = b.device("switch", WIDTH / 2 - 220, 340)
    dmz_switch = b.device("switch", WIDTH / 2 + 220, 340)
    b.link(cloud, firewall, nameif_b="outside", security_b=0)
    b.link(firewall, inside, nameif_a="inside", security_a=100)
    b.link(firewall, dmz_switch, nameif_a="dmz", security_a=50)
    for pc in b.row("pc", hosts, 490, 60, WIDTH / 2 - 20):
        b.link(inside, pc)
    for server in b.row("server", servers, 490, WIDTH / 2 + 20, WIDTH - 60):
        b.link(dmz_switch, server)


def ring(b: Builder, routers: int):
    nodes = []
    for index in range(routers):
        angle = -math.pi / 2 + 2 * math.pi * index / routers
        nodes.append(b.device("router", WIDTH / 2 + 300 * math.cos(angle), HEIGHT / 2 + 200 * math.sin(angle)))
    for index, node in enumerate(nodes):
        b.link(node, nodes[(index + 1) % routers], "routed")


def hub_spoke(b: Builder, spokes: int):
    hub = b.device("router", WIDTH / 2, 80)
    for spoke in b.row("router", spokes, 270):
        b.link(hub, spoke, "routed")
        pc = b.device("pc", spoke.pos_x, 440)
        b.link(spoke, pc)


def _param(name, label, default, low, high):
    return {"name": name, "label": label, "default": default, "min": low, "max": high}


BLUEPRINTS = {
    "network_star": {
        "label": "Star (L3 core + access)", "build": network_star,
        "description": "One L3 core switch with access switches connected by trunks.",
        "params": [_param("access", "Access switches", 3, 1, 24)],
    },
    "star": {
        "label": "Star (core + access)", "build": star,
        "description": "One router, switches around it, PCs on every switch.",
        "params": [_param("access", "Access switches", 3, 1, 8), _param("hosts", "PCs per switch", 2, 0, 6)],
    },
    "branch": {
        "label": "Branch office", "build": branch,
        "description": "Internet, one router, a switch and PCs.",
        "params": [_param("hosts", "PCs", 3, 0, 12)],
    },
    "campus": {
        "label": "Campus (3-tier)", "build": campus,
        "description": "Internet, firewall, two L3 distribution switches, redundant access switches.",
        "params": [_param("access", "Access switches", 4, 1, 8), _param("hosts", "PCs per switch", 2, 0, 4)],
    },
    "dmz": {
        "label": "Firewall + DMZ", "build": dmz,
        "description": "ASA with outside, inside and DMZ zones.",
        "params": [_param("hosts", "Inside PCs", 3, 0, 8), _param("servers", "DMZ servers", 2, 0, 8)],
    },
    "ring": {
        "label": "Router ring", "build": ring,
        "description": "Routers in a ring, each hop a routed /30.",
        "params": [_param("routers", "Routers", 4, 3, 10)],
    },
    "hub_spoke": {
        "label": "Hub and spoke", "build": hub_spoke,
        "description": "A hub router with spoke routers, each with a PC.",
        "params": [_param("spokes", "Spokes", 4, 2, 8)],
    },
}


class BlueprintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: dict[str, int] = Field(default_factory=dict)
    planned: bool = False


def catalog() -> list[dict]:
    return [
        {"key": key, "label": item["label"], "description": item["description"], "params": item["params"]}
        for key, item in BLUEPRINTS.items()
    ]


def build(project, key: str, payload: dict) -> dict:
    item = BLUEPRINTS.get(key)
    if item is None:
        raise NotFoundError(f"Unknown blueprint {key!r}.")
    request = validate(BlueprintRequest, payload, "Blueprint")
    values = {}
    for param in item["params"]:
        value = request.params.get(param["name"], param["default"])
        values[param["name"]] = max(param["min"], min(param["max"], value))
    builder = Builder(project, request.planned)
    try:
        item["build"](builder, **values)
    except AppError:
        _rollback(builder)
        raise
    return {
        "blueprint": key,
        "devices": [d.to_dict() for d in builder.devices],
        "links": [link.to_dict() for link in builder.links],
    }


def _rollback(builder: Builder) -> None:
    from ..extensions import db

    db.session.rollback()
    device_service.purge_devices([d.id for d in builder.devices])
