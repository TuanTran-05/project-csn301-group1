import re
def parse_ip_dhcp_pool(raw: str) -> list[dict]:
    rows=[]; current=None
    for line in raw.splitlines():
        m=re.match(r"Pool\s+(\S+)",line.strip(),re.I)
        if m: current={"name":m.group(1)}; rows.append(current); continue
        if current:
            m=re.search(r"network\s+(\S+)/(\d+)",line,re.I)
            if m: current["network"]=f"{m.group(1)}/{m.group(2)}"
            for key,label in (("leased","Leased addresses"),("excluded","Excluded addresses"),("total","Total addresses")):
                m=re.search(label+r"\s*:\s*(\d+)",line,re.I)
                if m: current[key]=int(m.group(1))
    return rows
