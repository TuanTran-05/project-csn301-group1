"""Parser for `show ip interface brief`."""

import re

# Interface  IP-Address  OK?  Method  Status  Protocol
# The status column may be "administratively down", so it is matched
# non-greedily and the protocol is anchored to the end of the line.
ROW = re.compile(
    r"^(?P<interface>\S+)\s+"
    r"(?P<ip_address>\S+)\s+"
    r"(?P<ok>YES|NO)\s+"
    r"(?P<method>\S+)\s+"
    r"(?P<status>.+?)\s+"
    r"(?P<protocol>up|down)\s*$",
    re.IGNORECASE,
)


def parse_ip_interface_brief(raw: str | None) -> list[dict]:
    if not raw:
        return []

    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("Interface"):
            continue

        match = ROW.match(line.strip())
        if match is None:
            continue

        rows.append(
            {
                "interface": match.group("interface"),
                "ip_address": match.group("ip_address"),
                "status": re.sub(r"\s+", " ", match.group("status")).strip().lower(),
                "protocol": match.group("protocol").lower(),
            }
        )
    return rows
