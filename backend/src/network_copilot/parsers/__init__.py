import re

from .interfaces import parse_ip_interface_brief
from .ospf import parse_ospf_neighbors
from .routes import parse_ip_routes
from .vlans import parse_vlan_brief
from .switchports import normalize_interface_name, normalize_vlan_set, parse_switchport_detail, parse_interfaces_trunk

# Maps a normalised command to the parser that understands its output.
PARSERS = {
    "show ip interface brief": parse_ip_interface_brief,
    "show vlan brief": parse_vlan_brief,
    "show ip route": parse_ip_routes,
    "show ip ospf neighbor": parse_ospf_neighbors,
    "show interfaces trunk": parse_interfaces_trunk,
}

PARAMETERIZED_PARSERS = ((re.compile(r"^show interfaces [A-Za-z][A-Za-z-]*\d[\d/.:]* switchport$", re.I), parse_switchport_detail),)


def parse_command_output(command: str, raw: str) -> list[dict] | None:
    """Parse known command output. Returns None when no parser is registered.

    Callers must always persist the raw output as well: a parser returning an
    empty list is not proof that the device reported nothing.
    """
    import re
    normalized = " ".join(command.strip().split()).casefold()
    parser = PARSERS.get(normalized)
    if parser is None:
        for pattern, candidate in PARAMETERIZED_PARSERS:
            if pattern.fullmatch(normalized): parser = candidate; break
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
    "normalize_interface_name", "normalize_vlan_set", "parse_switchport_detail", "parse_interfaces_trunk",
]
import re
