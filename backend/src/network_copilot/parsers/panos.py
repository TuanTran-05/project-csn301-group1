"""Parsers for Palo Alto PAN-OS operational commands.

* ``show system info``    -> one record of ``key: value`` facts
* ``show interface all``  -> one record per interface (hardware + logical rows)
* ``show routing route``  -> one record per route, per virtual router

PAN-OS tables are fixed-width and some columns can be empty (an interface
with no zone), so the logical-interface table is cut at the column positions
its dashed underline gives rather than split on whitespace.
"""

import re

_KEY_VALUE = re.compile(r"^\s*([A-Za-z0-9][\w.\-]*(?:\s[\w.\-]+)*)\s*:\s*(.*?)\s*$")


def parse_panos_system_info(raw: str) -> list[dict]:
    facts: dict[str, str] = {}
    for line in (raw or "").splitlines():
        match = _KEY_VALUE.match(line)
        if match and not line.lstrip().startswith(("#", ">")):
            facts[match.group(1).strip()] = match.group(2)
    return [facts] if facts else []


_HARDWARE_ROW = re.compile(
    r"^(?P<name>(?:ethernet|ae|ha|mgmt|loopback|tunnel|vlan)\S*)\s+(?P<id>\d+)\s+"
    r"(?P<sds>\S+/\S+/\S+)\s*(?P<mac>[0-9a-f]{2}(?::[0-9a-f]{2}){5})?\s*$",
    re.I,
)


def _column_spans(underline: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"-+", underline)]


def _cut(line: str, spans: list[tuple[int, int]]) -> list[str]:
    cells = []
    for index, (start, end) in enumerate(spans):
        stop = spans[index + 1][0] if index + 1 < len(spans) else len(line)
        cells.append(line[start:stop].strip())
    return cells


def parse_panos_interfaces(raw: str) -> list[dict]:
    lines = (raw or "").splitlines()
    rows: dict[str, dict] = {}
    order: list[str] = []

    def row(name: str) -> dict:
        if name not in rows:
            rows[name] = {"interface": name}
            order.append(name)
        return rows[name]

    logical_header, spans = None, None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("name") and "zone" in stripped.lower():
            logical_header = stripped
            nxt = lines[index + 1] if index + 1 < len(lines) else ""
            spans = _column_spans(nxt) if set(nxt.strip()) <= {"-", " "} and "-" in nxt else None
            continue
        if set(stripped) <= {"-", " "}:
            continue
        if logical_header and spans and len(spans) >= 6:
            cells = _cut(line, spans)
            name = cells[0]
            if not re.match(r"^[A-Za-z]", name or ""):
                continue
            item = row(name)
            item.update({"id": cells[1], "vsys": cells[2], "zone": cells[3] or None,
                         "forwarding": cells[4], "tag": cells[5]})
            address = cells[6] if len(cells) > 6 else ""
            if address and address.upper() != "N/A":
                ip, _, prefix = address.partition("/")
                item["ip_address"] = ip
                item["prefix_length"] = int(prefix) if prefix.isdigit() else None
            else:
                item.setdefault("ip_address", None)
            continue
        match = _HARDWARE_ROW.match(stripped)
        if match:
            speed, duplex, state = match.group("sds").split("/", 2)
            item = row(match.group("name"))
            item.update({"id": match.group("id"), "speed": speed, "duplex": duplex,
                         "state": state, "mac": match.group("mac"),
                         "status": state, "protocol": state})
    for item in rows.values():
        item.setdefault("ip_address", None)
        item.setdefault("status", None)
    return [rows[name] for name in order]


_FLAG_TOKENS = {"A", "?", "C", "H", "S", "~", "R", "O", "B", "Oi", "Oo", "O1", "O2", "E", "M"}
_PROTOCOLS = (("C", "connected"), ("H", "local"), ("S", "static"), ("B", "bgp"), ("R", "rip"))
_ROUTE_START = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\s")
_VIRTUAL_ROUTER = re.compile(r"^\s*VIRTUAL ROUTER:\s*(\S+)", re.I)


def parse_panos_routes(raw: str) -> list[dict]:
    routes = []
    router = None
    for line in (raw or "").splitlines():
        header = _VIRTUAL_ROUTER.match(line)
        if header:
            router = header.group(1)
            continue
        if not _ROUTE_START.match(line):
            continue
        tokens = line.split()
        destination, rest = tokens[0], tokens[1:]
        nexthop = rest.pop(0) if rest else None
        metric = int(rest.pop(0)) if rest and rest[0].isdigit() else None
        flags = []
        while rest and rest[0] in _FLAG_TOKENS:
            flags.append(rest.pop(0))
        age = int(rest.pop(0)) if rest and rest[0].isdigit() else None
        interface = rest.pop(0) if rest else None
        next_as = rest.pop(0) if rest else None
        protocol = "ospf" if any(f.startswith("O") for f in flags) else next(
            (name for flag, name in _PROTOCOLS if flag in flags), None)
        network, _, prefix = destination.partition("/")
        routes.append({
            "virtual_router": router,
            "destination": destination,
            "network": network,
            "prefix_length": int(prefix),
            "next_hop": None if nexthop in (None, "0.0.0.0", "discard") else nexthop,
            "metric": metric,
            "flags": flags,
            "active": "A" in flags,
            "protocol": protocol,
            "age": age,
            "interface": interface,
            "next_as": next_as,
        })
    return routes
