"""Interface-name handling shared by imports, discovery and comparison."""

import re

# Longest first, so "gigabitethernet" is not read as "g" + "igabit...".
_WORDS = (
    ("tengigabitethernet", "TenGigabitEthernet"),
    ("tengigabit", "TenGigabitEthernet"),
    ("tengig", "TenGigabitEthernet"),
    ("gigabitethernet", "GigabitEthernet"),
    ("fastethernet", "FastEthernet"),
    ("ethernet", "Ethernet"),
    ("gig", "GigabitEthernet"),
    ("gi", "GigabitEthernet"),
    ("fa", "FastEthernet"),
    ("te", "TenGigabitEthernet"),
    ("eth", "Ethernet"),
    ("g", "GigabitEthernet"),
    ("e", "Ethernet"),
)


def normalize_label(label: str | None) -> str | None:
    """"e0/1", "Gi 0/1", "gigabitethernet0/1" -> "GigabitEthernet0/1", or None."""
    text = "".join((label or "").split())
    match = re.match(r"^([A-Za-z-]+)(\d[\d/.:]*)$", text)
    if not match:
        return None
    word, rest = match.group(1).lower(), match.group(2)
    for prefix, full in _WORDS:
        if word == prefix:
            return f"{full}{rest}"
    return None


def same_interface(a: str | None, b: str | None) -> bool:
    """Whether two spellings name the same port (Gi0/1 == GigabitEthernet0/1)."""
    na, nb = normalize_label(a), normalize_label(b)
    if na is None or nb is None:
        return (a or "").strip().lower() == (b or "").strip().lower()
    return na.lower() == nb.lower()
