import re, ipaddress
def parse_ip_dhcp_pool(raw: str) -> list[dict]:
    rows=[]; current=None
    for line in raw.splitlines():
        m=re.match(r"Pool\s+(\S+)",line.strip(),re.I)
        if m: current={"name":m.group(1)}; rows.append(current); continue
        if current:
            m=re.search(r"network\s+(\d+(?:\.\d+){3})(?:/(\d+)|\s+(\d+(?:\.\d+){3}))",line,re.I)
            if m:
                try: current["network"]=str(ipaddress.IPv4Network(f"{m.group(1)}/{m.group(2) or m.group(3)}",strict=False))
                except ValueError: pass
            for key,label in (("leased","Leased addresses"),("excluded","Excluded addresses"),("total","Total addresses")):
                m=re.search(label+r"\s*:?\s*(\d+)",line,re.I)
                if m: current[key]=int(m.group(1))
    return rows
