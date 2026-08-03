import re
def parse_access_lists(raw: str) -> list[dict]:
    rows=[]; current=None
    for line in raw.splitlines():
        m=re.match(r"Standard IP access list\s+(.+)",line.strip(),re.I)
        if m: current={"name":m.group(1).strip(),"type":"standard","rules":[]}; rows.append(current); continue
        m=re.match(r"(\d+)\s+(permit|deny)\s+(.+)",line.strip(),re.I)
        if m and current:
            source=m.group(3).strip(); wildcard=None
            if ", wildcard bits " in source: source,wildcard=source.split(", wildcard bits ",1)
            current["rules"].append({"sequence":int(m.group(1)),"action":m.group(2).lower(),"source":source,"wildcard":wildcard})
    return rows
