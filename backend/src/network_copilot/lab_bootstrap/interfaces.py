"""Pure interface-label normalization used by the offline bootstrap core."""

import re


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
    """Normalize a supported short interface label without guessing unknown labels."""
    text = "".join((label or "").split())
    match = re.match(r"^([A-Za-z-]+)(\d[\d/.:]*)$", text)
    if not match:
        return None
    word, rest = match.group(1).lower(), match.group(2)
    for prefix, full in _WORDS:
        if word == prefix:
            return f"{full}{rest}"
    return None
