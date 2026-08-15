"""Parser for the Cisco ASA `show route` command.

ASA prints a netmask where IOS prints a prefix length, and the interface's
`nameif` (OUTSIDE, INSIDE, DMZ, MGMT) where IOS prints a physical
interface name. Measured against real FW-01 output, feeding ASA text to
parsers/routes.py silently yields /32 networks and a null interface - no
exception, just wrong data - so the two never share a parser.
"""

import ipaddress
import re

# "C        10.10.10.0 255.255.255.0 is directly connected, MGMT"
# "S*       0.0.0.0 0.0.0.0 [1/0] via 10.255.0.1, OUTSIDE"
ROUTE = re.compile(
    r"^(?P<protocol>[A-Za-z]{1,2}\*?"
    r"(?:\s+(?:IA|EX|E1|E2|N1|N2|L1|L2))?)\s+"
    r"(?P<network>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<netmask>\d{1,3}(?:\.\d{1,3}){3})\s*"
    r"(?P<rest>.*)$"
)

METRIC = re.compile(r"\[(?P<distance>\d+)/(?P<metric>\d+)\]")
VIA = re.compile(r"via\s+(?P<next_hop>\d{1,3}(?:\.\d{1,3}){3})")

SKIP_PREFIXES = ("Codes:", "Gateway of last resort")


def parse_asa_routes(raw: str | None) -> list[dict]:
    if not raw:
        return []

    rows: list[dict] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(SKIP_PREFIXES):
            continue

        match = ROUTE.match(stripped)
        if match is None:
            # Legend continuation lines carry no address pair and fall out
            # here, the same way the IOS parser ignores them.
            continue

        try:
            network = ipaddress.IPv4Network(
                f"{match.group('network')}/{match.group('netmask')}", strict=False
            )
        except ValueError:  # pragma: no cover - defensive against odd output
            continue

        rest = match.group("rest")
        metric = METRIC.search(rest)
        via = VIA.search(rest)

        interface = None
        if "," in rest:
            tail = rest.rsplit(",", 1)[1].strip()
            if tail:
                interface = tail

        rows.append(
            {
                "network": str(network),
                "protocol": re.sub(r"\s+", " ", match.group("protocol"))
                .replace("*", "")
                .strip(),
                "next_hop": via.group("next_hop") if via else None,
                "interface": interface,
                "distance": int(metric.group("distance")) if metric else None,
                "metric": int(metric.group("metric")) if metric else None,
            }
        )

    return rows
