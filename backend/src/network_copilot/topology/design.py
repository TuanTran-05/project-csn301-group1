"""Design intent beyond cabling: VLANs, access ports, SVIs, static routes, OSPF."""

import ipaddress


from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..devices.model import Device
from ..errors import ConflictError, NotFoundError, ValidationError
from ..extensions import db
from ..projects.service import validate

from .model import (
    DesignAccessPort,
    DesignOspf,
    DesignStaticRoute,
    DesignSvi,
    DesignVlan,
    TopologyLink,
)
from .interfaces import normalize_label, same_interface
from .schemas import normalize_interface

SWITCH_ROLES = ("access", "distribution")
RESERVED_VLANS = {1, 1002, 1003, 1004, 1005}


# -- schemas ------------------------------------------------------------------


class VlanSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vlan_id: int = Field(ge=2, le=4094)
    name: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("vlan_id")
    @classmethod
    def not_reserved(cls, value: int) -> int:
        if value in RESERVED_VLANS:
            raise ValueError("VLANs 1 and 1002-1005 are reserved")
        return value


class AccessPortSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: int
    interface: str = Field(min_length=1, max_length=40)
    vlan_id: int

    @field_validator("interface")
    @classmethod
    def check_interface(cls, value: str) -> str:
        # Stored in full form (GigabitEthernet0/5) however it was typed.
        value = normalize_interface(value)
        return normalize_label(value) or value


class SviSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: int
    vlan_id: int
    ip: str
    prefix_length: int = Field(ge=8, le=30)


class RouteSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: int
    network: str
    next_hop: str


class OspfSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: int
    process_id: int = Field(default=1, ge=1, le=65535)
    router_id: str | None = None


# -- helpers ------------------------------------------------------------------


def _device(project, device_id, *, roles=None, label="device", allow_asa=False) -> Device:
    device = db.session.get(Device, device_id)
    if device is None or device.project_id != project.id:
        raise ValidationError(f"The {label} is not a device of this project.")
    if device.device_type != "cisco_ios" and not allow_asa:
        raise ValidationError(f"{device.hostname} is not a Cisco IOS device; this setting only supports IOS.")
    if roles and device.role not in roles:
        raise ValidationError(
            f"{device.hostname} is a {device.role}; this setting is for {' / '.join(roles)} devices."
        )
    return device


def _ipv4(value: str, label: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValidationError(f"{label} must be a valid IPv4 address.", {label: [str(exc)]}) from exc


def _require_vlan(project, vlan_id: int) -> DesignVlan:
    vlan = (
        db.session.query(DesignVlan)
        .filter_by(project_id=project.id, vlan_id=vlan_id)
        .one_or_none()
    )
    if vlan is None:
        raise ValidationError(f"VLAN {vlan_id} is not defined in this project; add it first.")
    return vlan


def _used_networks(project, exclude_svi_vlan: int | None = None):
    """(network, owner description, vlan id or None) already taken by the design."""
    taken = [(ipaddress.ip_network(project.management_network, strict=False),
              "the management network", None)]
    for link in db.session.query(TopologyLink).filter(
        TopologyLink.project_id == project.id, TopologyLink.network.isnot(None)
    ):
        taken.append((ipaddress.ip_network(link.network), f"link {link.id}", None))
    for svi in db.session.query(DesignSvi).filter_by(project_id=project.id):
        taken.append((ipaddress.ip_network(f"{svi.ip}/{svi.prefix_length}", strict=False),
                      f"VLAN {svi.vlan_id} on {svi.device.hostname}", svi.vlan_id))
    return taken


# -- create ---------------------------------------------------------------------


def _make_vlan(project, payload):
    data = validate(VlanSchema, payload, "VLAN")
    if db.session.query(DesignVlan).filter_by(project_id=project.id, vlan_id=data.vlan_id).first():
        raise ConflictError(f"VLAN {data.vlan_id} already exists.")
    return DesignVlan(project_id=project.id, vlan_id=data.vlan_id, name=data.name)


def _make_access_port(project, payload):
    data = validate(AccessPortSchema, payload, "Access port")
    device = _device(project, data.device_id, roles=SWITCH_ROLES)
    _require_vlan(project, data.vlan_id)
    cabled = [
        link for link in db.session.query(TopologyLink).filter(
            db.or_(TopologyLink.device_a_id == device.id, TopologyLink.device_b_id == device.id))
        if same_interface(data.interface, link.interface_a if link.device_a_id == device.id else link.interface_b)
    ]
    if cabled:
        raise ConflictError(f"{data.interface} on {device.hostname} is already cabled to another device.")
    if db.session.query(DesignAccessPort).filter(
        DesignAccessPort.device_id == device.id,
        db.func.lower(DesignAccessPort.interface) == data.interface.lower(),
    ).first():
        raise ConflictError(f"{data.interface} on {device.hostname} is already an access port.")
    return DesignAccessPort(project_id=project.id, device_id=device.id,
                            interface=data.interface, vlan_id=data.vlan_id)


def _make_svi(project, payload):
    data = validate(SviSchema, payload, "SVI")
    device = _device(project, data.device_id, roles=SWITCH_ROLES)
    _require_vlan(project, data.vlan_id)
    address = _ipv4(data.ip, "ip")
    network = ipaddress.ip_network(f"{address}/{data.prefix_length}", strict=False)
    if address in (network.network_address, network.broadcast_address):
        raise ValidationError("ip must be a host address, not the network or broadcast address.")
    if db.session.query(DesignSvi).filter_by(device_id=device.id, vlan_id=data.vlan_id).first():
        raise ConflictError(f"{device.hostname} already has an interface for VLAN {data.vlan_id}.")
    for other, owner, other_vlan in _used_networks(project):
        # Switches sharing one VLAN legitimately share its subnet.
        if other_vlan == data.vlan_id and other == network:
            continue
        if network.overlaps(other):
            raise ConflictError(f"{network} overlaps {other} used by {owner}.")
    return DesignSvi(project_id=project.id, device_id=device.id, vlan_id=data.vlan_id,
                     ip=str(address), prefix_length=data.prefix_length)


def _make_route(project, payload):
    data = validate(RouteSchema, payload, "Route")
    device = _device(project, data.device_id, allow_asa=True)
    try:
        network = ipaddress.IPv4Network(data.network, strict=False)
    except ValueError as exc:
        raise ValidationError("network must be a valid IPv4 CIDR.", {"network": [str(exc)]}) from exc
    next_hop = _ipv4(data.next_hop, "next_hop")
    duplicate = db.session.query(DesignStaticRoute).filter_by(
        device_id=device.id, network=str(network.network_address),
        prefix_length=network.prefixlen, next_hop=str(next_hop)).first()
    if duplicate:
        raise ConflictError("That route already exists.")
    return DesignStaticRoute(project_id=project.id, device_id=device.id,
                             network=str(network.network_address),
                             prefix_length=network.prefixlen, next_hop=str(next_hop))


def _make_ospf(project, payload):
    data = validate(OspfSchema, payload, "OSPF")
    device = _device(project, data.device_id)
    if data.router_id:
        _ipv4(data.router_id, "router_id")
    existing = db.session.query(DesignOspf).filter_by(device_id=device.id).one_or_none()
    if existing is not None:  # one process per device: setting it again edits it
        existing.process_id = data.process_id
        existing.router_id = data.router_id
        return existing
    return DesignOspf(project_id=project.id, device_id=device.id,
                      process_id=data.process_id, router_id=data.router_id)


KINDS = {
    "vlans": (DesignVlan, _make_vlan),
    "access-ports": (DesignAccessPort, _make_access_port),
    "svis": (DesignSvi, _make_svi),
    "routes": (DesignStaticRoute, _make_route),
    "ospf": (DesignOspf, _make_ospf),
}


def _kind(kind: str):
    if kind not in KINDS:
        raise NotFoundError(f"Unknown design item {kind!r}.")
    return KINDS[kind]


def create_item(project, kind: str, payload: dict):
    _model, make = _kind(kind)
    item = make(project, payload)
    db.session.add(item)
    db.session.commit()
    return item


def delete_item(project, kind: str, item_id: int) -> None:
    model, _make = _kind(kind)
    item = db.session.get(model, item_id)
    if item is None or item.project_id != project.id:
        raise NotFoundError(f"{kind} item {item_id} was not found.")
    if isinstance(item, DesignVlan):
        users = (
            [f"access port {p.interface} on {p.device.hostname}"
             for p in db.session.query(DesignAccessPort).filter_by(project_id=project.id, vlan_id=item.vlan_id)]
            + [f"SVI on {s.device.hostname}"
               for s in db.session.query(DesignSvi).filter_by(project_id=project.id, vlan_id=item.vlan_id)]
        )
        if users:
            raise ConflictError(f"VLAN {item.vlan_id} is still used.", {"used_by": users})
    db.session.delete(item)
    db.session.commit()


def list_design(project) -> dict:
    def rows(model):
        return [i.to_dict() for i in db.session.query(model).filter_by(project_id=project.id).order_by(model.id)]

    return {
        "vlans": sorted(rows(DesignVlan), key=lambda v: v["vlan_id"]),
        "access_ports": rows(DesignAccessPort),
        "svis": rows(DesignSvi),
        "routes": rows(DesignStaticRoute),
        "ospf": rows(DesignOspf),
    }


# -- export / import ------------------------------------------------------------


def export_design(project) -> dict:
    design = list_design(project)
    return {
        "vlans": [{"vlan_id": v["vlan_id"], "name": v["name"]} for v in design["vlans"]],
        "access_ports": [{"hostname": p["hostname"], "interface": p["interface"], "vlan_id": p["vlan_id"]}
                         for p in design["access_ports"]],
        "svis": [{"hostname": s["hostname"], "vlan_id": s["vlan_id"], "ip": s["ip"],
                  "prefix_length": s["prefix_length"]} for s in design["svis"]],
        "routes": [{"hostname": r["hostname"], "network": f"{r['network']}/{r['prefix_length']}",
                    "next_hop": r["next_hop"]} for r in design["routes"]],
        "ospf": [{"hostname": o["hostname"], "process_id": o["process_id"], "router_id": o["router_id"]}
                 for o in design["ospf"]],
    }


def import_design(project, design: dict, by_hostname: dict) -> None:
    """Recreate exported design items; hostnames resolve through ``by_hostname``."""
    def device_id(item):
        device = by_hostname.get(str(item.get("hostname", "")).upper())
        if device is None:
            raise ValidationError(f"The design names an unknown device {item.get('hostname')!r}.")
        return device.id

    for item in design.get("vlans", []):
        create_item(project, "vlans", {"vlan_id": item.get("vlan_id"), "name": item.get("name")})
    for item in design.get("access_ports", []):
        create_item(project, "access-ports", {"device_id": device_id(item),
                    "interface": item.get("interface"), "vlan_id": item.get("vlan_id")})
    for item in design.get("svis", []):
        create_item(project, "svis", {"device_id": device_id(item), "vlan_id": item.get("vlan_id"),
                    "ip": item.get("ip"), "prefix_length": item.get("prefix_length")})
    for item in design.get("routes", []):
        create_item(project, "routes", {"device_id": device_id(item), "network": item.get("network"),
                    "next_hop": item.get("next_hop")})
    for item in design.get("ospf", []):
        create_item(project, "ospf", {"device_id": device_id(item),
                    "process_id": item.get("process_id", 1), "router_id": item.get("router_id")})


