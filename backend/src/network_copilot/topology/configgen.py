"""Turn a project's topology design into per-device configuration.

Nothing here talks to a device or writes the database. The result is a plan
(commands per device); ``create_design_preview`` hands the plan to the normal
change workflow, so every generated change still goes through
Preview -> Approve -> Apply -> Verify like any other.
"""

import ipaddress

from ..changes import batch_service
from ..changes.batch_service import BatchOperation
from ..errors import ValidationError
from .service import list_links

# Only IOS syntax is generated. ASA interfaces need a nameif and security
# level the design does not carry, so those devices are reported, not guessed.
SUPPORTED_DEVICE_TYPES = {"cisco_ios"}


def _endpoint_commands(link, side: str) -> tuple[object, list[str]]:
    if side == "a":
        device, interface = link.device_a, link.interface_a
        peer, peer_interface = link.device_b, link.interface_b
        address = link.ip_a
    else:
        device, interface = link.device_b, link.interface_b
        peer, peer_interface = link.device_a, link.interface_a
        address = link.ip_b

    label = link.description or f"LINK TO {peer.hostname} {peer_interface}"
    commands = [f"interface {interface}", f"description {label}"]

    if link.link_type == "routed":
        network = ipaddress.ip_network(link.network)
        commands.append(f"ip address {address} {network.netmask}")
    elif link.link_type == "trunk":
        commands.append("switchport mode trunk")
        if link.allowed_vlans:
            commands.append(f"switchport trunk allowed vlan {link.allowed_vlans}")

    if link.bring_up:
        commands.append("no shutdown")
    return device, commands


def build_plan(project, link_ids: list[int] | None = None) -> dict:
    """Commands per device for the chosen links (all links by default)."""
    links = list_links(project.id)
    if link_ids is not None:
        wanted = set(link_ids)
        unknown = wanted - {link.id for link in links}
        if unknown:
            raise ValidationError(
                "Unknown link ids.", {"link_ids": sorted(unknown)}
            )
        links = [link for link in links if link.id in wanted]
    if not links:
        raise ValidationError("The design has no links to configure.")

    per_device: dict[int, dict] = {}
    skipped: dict[int, dict] = {}
    for link in links:
        for side in ("a", "b"):
            device, commands = _endpoint_commands(link, side)
            if device.device_type not in SUPPORTED_DEVICE_TYPES:
                skipped[device.id] = {
                    "device_id": device.id,
                    "hostname": device.hostname,
                    "reason": (
                        f"Configuration generation does not support "
                        f"{device.device_type}."
                    ),
                }
                continue
            entry = per_device.setdefault(
                device.id,
                {
                    "device_id": device.id,
                    "hostname": device.hostname,
                    "commands": [],
                    "link_ids": [],
                },
            )
            entry["commands"].extend(commands)
            entry["link_ids"].append(link.id)

    devices = sorted(per_device.values(), key=lambda item: item["hostname"])
    return {
        "devices": devices,
        "skipped": sorted(skipped.values(), key=lambda item: item["hostname"]),
    }


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
