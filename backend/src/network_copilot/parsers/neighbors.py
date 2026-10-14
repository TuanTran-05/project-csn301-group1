"""Parse ``show cdp neighbors detail`` and ``show lldp neighbors detail``."""

import re


def _blocks(output: str) -> list[str]:
    return [b for b in re.split(r"^-{5,}\s*$", output or "", flags=re.M) if b.strip()]


def parse_cdp_detail(output: str) -> list[dict]:
    neighbors = []
    for block in _blocks(output):
        device = re.search(r"^\s*Device ID:\s*(\S+)", block, re.M | re.I)
        interface = re.search(
            r"^\s*Interface:\s*([^,\s]+)\s*,\s*Port ID \(outgoing port\):\s*(\S+)",
            block, re.M | re.I,
        )
        if not device or not interface:
            continue
        ip = re.search(r"IP(?:v4)? address:\s*(\d+\.\d+\.\d+\.\d+)", block, re.I)
        platform = re.search(r"^\s*Platform:\s*([^,\n]+)", block, re.M | re.I)
        neighbors.append(
            {
                "protocol": "cdp",
                "neighbor": device.group(1),
                "local_interface": interface.group(1),
                "remote_interface": interface.group(2),
                "ip": ip.group(1) if ip else None,
                "platform": platform.group(1).strip() if platform else None,
            }
        )
    return neighbors


def parse_lldp_detail(output: str) -> list[dict]:
    neighbors = []
    for block in _blocks(output):
        local = re.search(r"^\s*Local Intf:\s*(\S+)", block, re.M | re.I)
        port = re.search(r"^\s*Port id:\s*(\S+)", block, re.M | re.I)
        name = re.search(r"^\s*System Name:\s*(\S+)", block, re.M | re.I)
        if not (local and port and name):
            continue
        ip = re.search(r"IP:\s*(\d+\.\d+\.\d+\.\d+)", block, re.I)
        neighbors.append(
            {
                "protocol": "lldp",
                "neighbor": name.group(1),
                "local_interface": local.group(1),
                "remote_interface": port.group(1),
                "ip": ip.group(1) if ip else None,
                "platform": None,
            }
        )
    return neighbors
