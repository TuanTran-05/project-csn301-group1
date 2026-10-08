"""Topology design: devices on a canvas, cabled together by links."""

import ipaddress

from ..devices.model import Device
from ..errors import ConflictError, NotFoundError, ValidationError
from ..extensions import db
from ..projects.service import validate
from .model import TopologyLink
from .schemas import LayoutSchema, LinkSchema, LinkUpdateSchema, QuickDeviceSchema
from .templates import get_template, next_free_interface, next_hostname, next_management_ip

MIN_ROUTED_PREFIX = 8
MAX_ROUTED_PREFIX = 31


# -- reads ------------------------------------------------------------------


def list_links(project_id: int) -> list[TopologyLink]:
    return (
        db.session.query(TopologyLink)
        .filter(TopologyLink.project_id == project_id)
        .order_by(TopologyLink.id)
        .all()
    )


def get_link(project_id: int, link_id: int) -> TopologyLink:
    link = db.session.get(TopologyLink, link_id)
    if link is None or link.project_id != project_id:
        raise NotFoundError(f"Link {link_id} was not found.")
    return link


def get_topology(project) -> dict:
    devices = (
        db.session.query(Device)
        .filter(Device.project_id == project.id)
        .order_by(Device.hostname, Device.id)
        .all()
    )
    return {
        "project": project.to_dict(),
        "nodes": [device.to_dict() for device in devices],
        "links": [link.to_dict() for link in list_links(project.id)],
    }


# -- validation -------------------------------------------------------------


def _project_device(project_id: int, device_id: int) -> Device:
    device = db.session.get(Device, device_id)
    if device is None or device.project_id != project_id:
        raise ValidationError(
            "Link endpoints must be devices of this project.",
            {"device_id": [f"Device {device_id} is not in this project."]},
        )
    return device


def normalize_vlan_list(value: str) -> str:
    """Canonical "10,20,30-40" form of an IOS VLAN list, or ValidationError."""
    vlans: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            raise ValidationError("allowed_vlans has an empty entry.")
        if "-" in token:
            parts = token.split("-")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise ValidationError(f"'{token}' is not a valid VLAN range.")
            start, end = int(parts[0]), int(parts[1])
            if start > end:
                raise ValidationError(f"'{token}' is not a valid VLAN range.")
            vlans.update(range(start, end + 1))
        elif token.isdigit():
            vlans.add(int(token))
        else:
            raise ValidationError(f"'{token}' is not a valid VLAN id.")
    if not vlans or min(vlans) < 1 or max(vlans) > 4094:
        raise ValidationError("VLAN ids must be between 1 and 4094.")

    ordered = sorted(vlans)
    ranges: list[str] = []
    start = previous = ordered[0]
    for vlan in ordered[1:] + [None]:
        if vlan is not None and vlan == previous + 1:
            previous = vlan
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        if vlan is not None:
            start = previous = vlan
    return ",".join(ranges)


def _resolve_routed(project, fields: dict, exclude_id: int | None) -> None:
    """Validate the subnet of a routed link and fill in missing addresses."""
    if not fields.get("network"):
        raise ValidationError(
            "A routed link needs a point-to-point network.",
            {"network": ["Required for routed links, e.g. 10.0.12.0/30."]},
        )
    try:
        network = ipaddress.IPv4Network(fields["network"], strict=False)
    except ValueError as exc:
        raise ValidationError(
            "network must be a valid IPv4 CIDR.", {"network": [str(exc)]}
        ) from exc
    if not MIN_ROUTED_PREFIX <= network.prefixlen <= MAX_ROUTED_PREFIX:
        raise ValidationError(
            f"A link network must be between /{MIN_ROUTED_PREFIX} and "
            f"/{MAX_ROUTED_PREFIX}."
        )

    management = ipaddress.ip_network(project.management_network, strict=False)
    if network.overlaps(management):
        raise ValidationError(
            f"network overlaps the management network {management}; data links "
            "must use their own range."
        )

    for other in list_links(project.id):
        if other.id == exclude_id or not other.network:
            continue
        if network.overlaps(ipaddress.ip_network(other.network)):
            raise ConflictError(
                f"network overlaps {other.network} used by another link."
            )

    usable = list(network.hosts())
    if len(usable) < 2:
        raise ValidationError("The network is too small to address both ends.")

    def pick(raw: str | None, default, label: str):
        if not raw:
            return default
        try:
            address = ipaddress.IPv4Address(raw)
        except ValueError as exc:
            raise ValidationError(
                f"{label} must be a valid IPv4 address.", {label: [str(exc)]}
            ) from exc
        if address not in usable:
            raise ValidationError(
                f"{label} must be a usable host address inside {network}.",
                {label: [f"{raw} is not a host address of {network}."]},
            )
        return address

    ip_a = pick(fields.get("ip_a"), usable[0], "ip_a")
    ip_b = pick(fields.get("ip_b"), usable[1], "ip_b")
    if ip_a == ip_b:
        raise ValidationError("ip_a and ip_b must be different addresses.")

    fields["network"] = str(network)
    fields["ip_a"] = str(ip_a)
    fields["ip_b"] = str(ip_b)
    fields["allowed_vlans"] = None


def _finalize_fields(project, fields: dict, exclude_id: int | None = None) -> dict:
    """Make the type-specific fields of a link consistent, or raise."""
    link_type = fields["link_type"]
    if link_type == "routed":
        _resolve_routed(project, fields, exclude_id)
    elif link_type == "trunk":
        if fields.get("network") or fields.get("ip_a") or fields.get("ip_b"):
            raise ValidationError("Only routed links carry a network or addresses.")
        fields["network"] = fields["ip_a"] = fields["ip_b"] = None
        if fields.get("allowed_vlans"):
            fields["allowed_vlans"] = normalize_vlan_list(fields["allowed_vlans"])
        else:
            fields["allowed_vlans"] = None
    else:
        if (
            fields.get("network")
            or fields.get("ip_a")
            or fields.get("ip_b")
            or fields.get("allowed_vlans")
        ):
            raise ValidationError(
                "A physical link carries no network, addresses or VLANs; make it "
                "routed or trunk."
            )
        fields["network"] = fields["ip_a"] = fields["ip_b"] = None
        fields["allowed_vlans"] = None
    return fields


def _assert_endpoints_free(
    project_id: int, endpoints: list[tuple[int, str]], exclude_id: int | None
) -> None:
    taken = {}
    for link in list_links(project_id):
        if link.id == exclude_id:
            continue
        taken[(link.device_a_id, link.interface_a.lower())] = link
        taken[(link.device_b_id, link.interface_b.lower())] = link
    for device_id, interface in endpoints:
        if (device_id, interface.lower()) in taken:
            raise ConflictError(
                f"Interface {interface} on device {device_id} already has a link."
            )


# -- writes -----------------------------------------------------------------


def create_link(project, payload: dict) -> TopologyLink:
    data = validate(LinkSchema, payload, "Link")
    if data.device_a_id == data.device_b_id:
        raise ValidationError("A link must connect two different devices.")
    device_a = _project_device(project.id, data.device_a_id)
    device_b = _project_device(project.id, data.device_b_id)
    fields = data.model_dump()
    if fields["interface_a"] is None:
        fields["interface_a"] = next_free_interface(project.id, device_a)
    if fields["interface_b"] is None:
        fields["interface_b"] = next_free_interface(project.id, device_b)
    _assert_endpoints_free(
        project.id,
        [
            (data.device_a_id, fields["interface_a"]),
            (data.device_b_id, fields["interface_b"]),
        ],
        None,
    )

    fields = _finalize_fields(project, fields)
    link = TopologyLink(project_id=project.id, **fields)
    db.session.add(link)
    db.session.commit()
    return link


def update_link(project, link_id: int, payload: dict) -> TopologyLink:
    link = get_link(project.id, link_id)
    data = validate(LinkUpdateSchema, payload, "Link")
    changes = data.model_dump(exclude_unset=True)

    fields = {
        "link_type": link.link_type,
        "network": link.network,
        "ip_a": link.ip_a,
        "ip_b": link.ip_b,
        "allowed_vlans": link.allowed_vlans,
    }
    # Changing the subnet re-derives the addresses unless the caller sets them.
    if "network" in changes and changes["network"] != link.network:
        fields["ip_a"] = fields["ip_b"] = None
    if "link_type" in changes and changes["link_type"] != link.link_type:
        fields["network"] = fields["ip_a"] = fields["ip_b"] = None
        fields["allowed_vlans"] = None
    fields.update(
        {k: changes[k] for k in changes if k in fields}
    )
    fields = _finalize_fields(project, fields, exclude_id=link.id)

    for key, value in fields.items():
        setattr(link, key, value)
    if "bring_up" in changes and changes["bring_up"] is not None:
        link.bring_up = changes["bring_up"]
    if "description" in changes:
        link.description = changes["description"]
    db.session.commit()
    return link


def delete_link(project, link_id: int) -> None:
    link = get_link(project.id, link_id)
    db.session.delete(link)
    db.session.commit()


def save_layout(project, payload: dict) -> list[Device]:
    data = validate(LayoutSchema, payload, "Layout")
    ids = [position.device_id for position in data.positions]
    if len(set(ids)) != len(ids):
        raise ValidationError("A device appears twice in the layout.")

    devices = {
        device.id: device
        for device in db.session.query(Device).filter(
            Device.project_id == project.id, Device.id.in_(ids)
        )
    }
    missing = sorted(set(ids) - set(devices))
    if missing:
        raise ValidationError(
            "Layout names devices outside this project.", {"device_ids": missing}
        )

    for position in data.positions:
        devices[position.device_id].pos_x = position.x
        devices[position.device_id].pos_y = position.y
    db.session.commit()
    return list(devices.values())


def quick_add_device(project, payload: dict) -> Device:
    """Create a device from a template with a free name and management IP.

    The name and address are suggestions chosen server-side so two quick drops
    never collide; the normal device validation still runs on the result.
    """
    from ..devices import service as device_service

    data = validate(QuickDeviceSchema, payload, "Device")
    template = get_template(data.template)
    device_payload = {
        "hostname": next_hostname(project.id, template["prefix"]),
        "management_ip": next_management_ip(project),
        "device_type": template["device_type"],
        "role": template["role"],
        "environment": data.environment
        or ("physical" if project.environment == "physical" else "pnetlab"),
    }
    if data.credential:
        device_payload["credential"] = data.credential
    if data.x is not None and data.y is not None:
        device_payload["pos_x"] = data.x
        device_payload["pos_y"] = data.y
    return device_service.create_device(device_payload, project)
