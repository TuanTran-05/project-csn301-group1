import re
from .switchports import normalize_interface_name

def normalize_ios_config(raw: str) -> tuple[str, ...]:
    noise = ("building configuration", "current configuration", "last configuration change", "nvram config last updated")
    lines=[]
    for line in raw.splitlines():
        s=line.rstrip()
        if not s.strip() or s.strip().casefold()=="end" or any(s.strip().casefold().startswith(n) for n in noise): continue
        lines.append(s)
    return tuple(lines)

def extract_interface_stanza(raw: str, interface: str) -> list[str]:
    target=normalize_interface_name(interface).casefold(); result=[]; active=False
    for line in normalize_ios_config(raw):
        if re.match(r"^interface\s+", line, re.I):
            active = line.split(None,1)[1].casefold() == target
        elif active and (line == "!" or (line and not line.startswith((" ", "\t")))):
            active=False
        if active: result.append(line)
    return result
