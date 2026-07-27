from .interfaces import parse_ip_interface_brief
from .ospf import parse_ospf_neighbors
from .routes import parse_ip_routes
from .vlans import parse_vlan_brief

# Maps a normalised command to the parser that understands its output.
PARSERS = {
    "show ip interface brief": parse_ip_interface_brief,
    "show vlan brief": parse_vlan_brief,
    "show ip route": parse_ip_routes,
    "show ip ospf neighbor": parse_ospf_neighbors,
}


def parse_command_output(command: str, raw: str) -> list[dict] | None:
    """Parse known command output. Returns None when no parser is registered.

    Callers must always persist the raw output as well: a parser returning an
    empty list is not proof that the device reported nothing.
    """
    parser = PARSERS.get(command)
    if parser is None:
        return None
    try:
        return parser(raw)
    except Exception:  # pragma: no cover - a parser must never break a poll
        return []


__all__ = [
    "PARSERS",
    "parse_command_output",
    "parse_ip_interface_brief",
    "parse_ip_routes",
    "parse_ospf_neighbors",
    "parse_vlan_brief",
]
