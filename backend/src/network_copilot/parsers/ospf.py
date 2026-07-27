"""Parser for `show ip ospf neighbor`."""

import re

# "2.2.2.2           1   FULL/DR         00:00:33    10.255.0.6   GigabitEthernet0/2"
# The state field can contain spaces ("FULL/  -"), so it is matched
# non-greedily between the priority and the dead timer.
ROW = re.compile(
    r"^(?P<neighbor_id>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<priority>\d+)\s+"
    r"(?P<state>\S+(?:\s+\S+)*?)\s+"
    r"(?P<dead_time>\d{2}:\d{2}:\d{2}|-)\s+"
    r"(?P<address>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<interface>\S+)\s*$"
)


def parse_ospf_neighbors(raw: str | None) -> list[dict]:
    if not raw:
        return []

    rows: list[dict] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Neighbor ID"):
            continue

        match = ROW.match(stripped)
        if match is None:
            continue

        rows.append(
            {
                "neighbor_id": match.group("neighbor_id"),
                "priority": int(match.group("priority")),
                # "FULL/  -" is reported by IOS on point-to-point links.
                "state": re.sub(r"\s+", "", match.group("state")),
                "dead_time": match.group("dead_time"),
                "address": match.group("address"),
                "interface": match.group("interface"),
            }
        )
    return rows
