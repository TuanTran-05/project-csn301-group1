"""Parser for `show ip route`."""

import re

# "S*    0.0.0.0/0 [1/0] via 10.255.0.1"
# "O IA  10.10.30.0/24 [110/3] via 10.255.0.6, 00:14:32, GigabitEthernet0/2"
# "C     10.10.10.0/24 is directly connected, GigabitEthernet0/1"
ROUTE = re.compile(
    r"^(?P<protocol>[A-Za-z]{1,2}\*?"
    r"(?:\s+(?:IA|EX|E1|E2|N1|N2|L1|L2))?)\s+"
    r"(?P<network>\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)\s*"
    r"(?P<rest>.*)$"
)

# "      10.0.0.0/8 is variably subnetted, 6 subnets, 3 masks"
SUBNETTED = re.compile(
    r"^\d{1,3}(?:\.\d{1,3}){3}/(?P<mask>\d{1,2})\s+is\s+(?:variably\s+)?subnetted"
)

METRIC = re.compile(r"\[(?P<distance>\d+)/(?P<metric>\d+)\]")
VIA = re.compile(r"via\s+(?P<next_hop>\d{1,3}(?:\.\d{1,3}){3})")
INTERFACE = re.compile(r"(?P<interface>[A-Za-z][A-Za-z\-]*\d[\d/.:]*)\s*$")

SKIP_PREFIXES = ("Codes:", "Gateway of last resort")


def parse_ip_routes(raw: str | None) -> list[dict]:
    if not raw:
        return []

    rows: list[dict] = []
    # Mask carried over from the most recent "is subnetted" header, used for
    # route lines that print a bare host address.
    inherited_mask: int | None = None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(SKIP_PREFIXES):
            continue

        subnetted = SUBNETTED.match(stripped)
        if subnetted is not None:
            inherited_mask = int(subnetted.group("mask"))
            continue

        match = ROUTE.match(stripped)
        if match is None:
            continue

        network = match.group("network")
        if "/" not in network:
            network = f"{network}/{inherited_mask if inherited_mask else 32}"

        rest = match.group("rest")
        metric = METRIC.search(rest)
        via = VIA.search(rest)

        interface = None
        if "," in rest:
            tail = rest.rsplit(",", 1)[1].strip()
            interface_match = INTERFACE.match(tail)
            if interface_match is not None:
                interface = interface_match.group("interface")

        rows.append(
            {
                "network": network,
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
