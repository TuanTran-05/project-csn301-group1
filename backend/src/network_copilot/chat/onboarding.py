"""Small, deterministic steps for creating projects from the chat composer."""

import re
import unicodedata


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.lower())
    return "".join(
        "d" if char == "đ" else char
        for char in normalized
        if unicodedata.category(char) != "Mn"
    )


def requests_project(message: str) -> bool:
    text = _fold(message)
    if any(phrase in text for phrase in (
        "tao project", "tao du an", "tao topology", "tao so do",
        "create a project", "create project", "new project",
        "design a network", "generate a topology", "build a topology",
    )):
        return True
    return bool(re.search(
        r"\b(?:tao|lap|create|new)\s+(?:(?:mot|a)\s+)?(?:project|du an|topology|so do)\b",
        text,
    ))


def requests_network_design(message: str) -> bool:
    """Recognize requests for a suggested network diagram, not device actions."""
    text = _fold(message)
    if any(phrase in text for phrase in (
        "so do", "network diagram", "network topology", "topology",
        "mo hinh mang",
    )):
        return True

    action = r"(?:tao|ve|thiet\s+ke|xay\s+dung|lap\s+dat|create|draw|design|build|set\s+up)"
    venue = r"(?:tiem\s+net|phong\s+net|phong\s+may|internet\s+cafe|cyber\s+cafe|computer\s+lab)"
    if re.search(rf"\b{action}\b(?:\s+\w+){{0,3}}\s+\b{venue}\b", text):
        return True

    # Keep a generic "network" target close to the design verb so requests
    # such as "create a VLAN on the guest network" remain device actions.
    return bool(re.search(
        rf"\b{action}\b\s+(?:(?:mot|a|an|new)\s+)?(?:mang|network)\b",
        text,
    ))


def requested_device_count(message: str) -> int | None:
    match = re.search(r"(?<!\d)(\d{1,2})(?!\d)", message)
    if not match:
        return None
    value = int(match.group(1))
    return value if 2 <= value <= 25 else None


def topology_options(count: int) -> list[dict]:
    """Candidate designs all use exactly count managed network devices."""
    options = [{
        "key": "network_star",
        "title": "Hình sao",
        "description": "Một switch L3 trung tâm nối trunk đến các switch access.",
        "params": {"access": count - 1},
        "device_count": count,
    }]
    if 3 <= count <= 10:
        options.append({
            "key": "ring", "title": "Vòng định tuyến",
            "description": "Các router nối thành vòng, mỗi đường liên kết dùng mạng /30.",
            "params": {"routers": count}, "device_count": count,
        })
    if 4 <= count <= 11:
        options.append({
            "key": "campus", "title": "Mạng campus dự phòng",
            "description": "Firewall, hai switch L3 dự phòng và các switch access nối kép.",
            "params": {"access": count - 3, "hosts": 0}, "device_count": count,
        })
    if 3 <= count <= 9:
        options.append({
            "key": "hub_spoke", "title": "Hub and spoke",
            "description": "Một router trung tâm kết nối đến các router chi nhánh.",
            "params": {"spokes": count - 1}, "device_count": count,
        })
    return options
