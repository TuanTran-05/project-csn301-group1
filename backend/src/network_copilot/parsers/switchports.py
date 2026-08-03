import re

ALIASES = {"gi":"GigabitEthernet","gigabitethernet":"GigabitEthernet","fa":"FastEthernet","fastethernet":"FastEthernet","te":"TenGigabitEthernet","tengigabitethernet":"TenGigabitEthernet","eth":"Ethernet","ethernet":"Ethernet","po":"Port-channel","port-channel":"Port-channel","vl":"Vlan","vlan":"Vlan","lo":"Loopback","loopback":"Loopback"}

def normalize_interface_name(value: str) -> str:
    if not isinstance(value, str) or value.strip() != value or any(c in value for c in ";|&`$><"):
        raise ValueError("invalid interface")
    m = re.fullmatch(r"([A-Za-z-]+)(\d[\d/.:]*)", value)
    if not m or m.group(1).casefold() not in ALIASES:
        raise ValueError("invalid interface")
    return ALIASES[m.group(1).casefold()] + m.group(2)

def normalize_vlan_set(value: str) -> list[int]:
    if value.strip().casefold() == "all": return list(range(1, 4095))
    result = set()
    for token in value.split(","):
        parts = token.strip().split("-")
        if len(parts) == 1 and parts[0].isdigit(): start = end = int(parts[0])
        elif len(parts) == 2 and all(p.isdigit() for p in parts): start, end = map(int, parts)
        else: raise ValueError("invalid VLAN set")
        if start > end or start < 1 or end > 4094: raise ValueError("invalid VLAN range")
        result.update(range(start, end + 1))
    return sorted(result)

def parse_switchport_detail(raw: str) -> list[dict]:
    row = {}
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        k = key.strip().casefold()
        if k == "name":
            row["interface"] = normalize_interface_name(value.strip())
        elif k == "administrative mode": row["administrative_mode"] = value.strip().casefold()
        elif k == "operational mode": row["operational_mode"] = value.strip().casefold()
        elif k == "access mode vlan": row["access_vlan"] = int(re.match(r"\d+", value.strip()).group())
        elif k == "trunking vlans enabled": row["allowed_vlans"] = normalize_vlan_set(value.strip())
    return [row] if row else []

def parse_interfaces_trunk(raw: str) -> list[dict]:
    rows=[]
    for line in raw.splitlines():
        parts=line.split()
        if len(parts) >= 6 and parts[1] == "on":
            try: rows.append({"interface": normalize_interface_name(parts[0]), "status": parts[3].casefold(), "allowed_vlans": normalize_vlan_set(" ".join(parts[5:]))})
            except ValueError: pass
    return rows
