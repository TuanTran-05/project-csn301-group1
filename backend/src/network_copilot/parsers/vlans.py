"""Parser for `show vlan brief`."""

import re

# 25   MARKETING                        active    Gi0/1, Gi0/2
ROW = re.compile(
    r"^(?P<vlan_id>\d{1,4})\s+"
    r"(?P<name>\S+)\s+"
    r"(?P<status>\S+)"
    r"(?P<ports>.*)$"
)

# Ports belonging to the previous VLAN, wrapped onto their own line.
CONTINUATION = re.compile(r"^\s+(?P<ports>[A-Za-z]{2,3}\d+(/\d+)*(\s*,\s*\S+)*)\s*$")


def _split_ports(raw: str) -> list[str]:
    return [port.strip() for port in raw.split(",") if port.strip()]


def parse_vlan_brief(raw: str | None) -> list[dict]:
    if not raw:
        return []

    rows: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("VLAN ") or stripped.startswith("----"):
            continue

        match = ROW.match(stripped)
        if match is not None:
            rows.append(
                {
                    "vlan_id": int(match.group("vlan_id")),
                    "name": match.group("name"),
                    "status": match.group("status"),
                    "ports": _split_ports(match.group("ports")),
                }
            )
            continue

        continuation = CONTINUATION.match(line)
        if continuation is not None and rows:
            rows[-1]["ports"].extend(_split_ports(continuation.group("ports")))

    return rows
