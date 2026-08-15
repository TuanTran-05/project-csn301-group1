import re

from .asa_routes import parse_asa_routes
from .interfaces import parse_ip_interface_brief
from .ospf import parse_ospf_neighbors
from .routes import parse_ip_routes
from .vlans import parse_vlan_brief
from .switchports import normalize_interface_name, normalize_vlan_set, parse_switchport_detail, parse_interfaces_trunk
from .config import extract_interface_stanza, normalize_ios_config
from .acls import parse_access_lists
from .dhcp import parse_ip_dhcp_pool

# Maps a normalised command to the parser that understands its output.
# "show interface ip brief" is the ASA spelling; its output format was
# measured to be identical to IOS, so it shares the IOS parser. "show route"
# is also ASA but needs its own parser - see parsers/asa_routes.py.
PARSERS = {
    "show ip interface brief": parse_ip_interface_brief,
    "show interface ip brief": parse_ip_interface_brief,
    "show vlan brief": parse_vlan_brief,
    "show ip route": parse_ip_routes,
    "show route": parse_asa_routes,
    "show ip ospf neighbor": parse_ospf_neighbors,
    "show interfaces trunk": parse_interfaces_trunk,
    "show access-lists": parse_access_lists,
    "show ip dhcp pool": parse_ip_dhcp_pool,
}

PARAMETERIZED_PARSERS = ((re.compile(r"^show interfaces [A-Za-z][A-Za-z-]*\d[\d/.:]* switchport$", re.I), parse_switchport_detail),)


def parse_command_output(command: str, raw: str) -> list[dict] | None:
    """Parse known command output. Returns None when no parser is registered.

    Callers must always persist the raw output as well: a parser returning an
    empty list is not proof that the device reported nothing.
    """
    normalized = " ".join(command.strip().split()).casefold()
    parser = PARSERS.get(normalized)
    if parser is None:
        for pattern, candidate in PARAMETERIZED_PARSERS:
            if pattern.fullmatch(normalized):
                parser = candidate
                break
    if parser is None:
        return None
    try:
        return parser(raw)
    except Exception:  # pragma: no cover - a parser must never break a poll
        return []


__all__ = [
    "PARSERS",
    "parse_asa_routes",
    "parse_command_output",
    "parse_ip_interface_brief",
    "parse_ip_routes",
    "parse_ospf_neighbors",
    "parse_vlan_brief",
    "normalize_interface_name",
    "normalize_vlan_set",
    "parse_switchport_detail",
    "parse_interfaces_trunk",
    "extract_interface_stanza",
    "normalize_ios_config",
    "parse_access_lists",
    "parse_ip_dhcp_pool",
]
