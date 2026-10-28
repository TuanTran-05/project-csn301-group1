"""Turn one Cisco IOS running-config into another.

``restore_commands(current, target)`` returns the configuration-mode lines
that change a device whose running-config is ``current`` into ``target``:
lines only in ``current`` are negated, lines only in ``target`` are added,
and sections present in both (``interface``, ``router ospf``, ``vlan``...)
are entered and compared line by line.

The comparison is structural (indentation), not textual, so line order inside
a section does not matter. Lines that cannot be restored safely from a text
config - secrets, banners, certificates - are never emitted; they are
reported in ``skipped`` so the operator sees what is left as it is.
This module never opens SSH and has no database access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Header and volatile lines that differ between two captures of the same config.
_VOLATILE = re.compile(
    r"^(Building configuration|Current configuration|! Last configuration change"
    r"|! NVRAM config last updated|! No configuration change since last restart"
    r"|ntp clock-period|end$)"
)

# Sections whose body is not configuration a CLI line can recreate.
_OPAQUE_SECTIONS = re.compile(r"^(crypto pki certificate chain|banner\s)")

# Lines whose value is a secret or that would change how we log in. They are
# compared but never emitted: a restore must not lock the backend out, and a
# secret must not end up in a change record.
_SENSITIVE = re.compile(
    r"^(username\s|enable\s+(secret|password)|crypto\s+key|snmp-server\s+community"
    r"|.*\b(password|secret|key-string|pre-shared-key|key\s+\d+\s)\b)",
    re.I,
)

# Commands that hold one value: adding the new value replaces the old one, so
# the old line is not negated first ("no hostname A" would rename the device
# to "Router" for a moment; "no ip address" would drop the interface).
_SINGLETON = re.compile(
    r"^(hostname|ip domain[- ]name|ip default-gateway|description|ip address(?!.*\ssecondary$)"
    r"|switchport mode|switchport access vlan|switchport trunk native vlan"
    r"|switchport trunk allowed vlan|switchport voice vlan|switchport trunk encapsulation"
    r"|speed|duplex|mtu|bandwidth|encapsulation|clock timezone|spanning-tree mode"
    r"|vtp mode|vtp domain|name|router-id|ip ospf cost|ip ospf priority|nameif|security-level)\b"
)


def _singleton_key(text: str) -> str | None:
    match = _SINGLETON.match(text)
    return match.group(1) if match else None


# Characters the change workflow refuses (see commands.policy.SHELL_METACHARACTERS).
_FORBIDDEN_CHARS = (";", "|", "&", "$(", "`", ">", "<")


@dataclass
class Node:
    text: str
    children: list["Node"] = field(default_factory=list)


@dataclass
class RestorePlan:
    commands: list[str]
    skipped: list[str]

    @property
    def empty(self) -> bool:
        return not self.commands


def _banner_delimiter(line: str) -> str | None:
    """IOS prints ``banner motd ^C...^C``; return the delimiter if the banner continues."""
    match = re.match(r"^banner\s+\S+\s+(\^C|\S)(.*)$", line)
    if not match:
        return None
    delimiter, rest = match.group(1), match.group(2)
    return None if delimiter in rest else delimiter


def parse(config: str) -> list[Node]:
    """Indentation tree of a running-config, comments and volatile lines removed."""
    roots: list[Node] = []
    stack: list[tuple[int, Node]] = []
    banner_end: str | None = None
    banner_node: Node | None = None

    for raw in (config or "").splitlines():
        line = raw.rstrip()
        if banner_end is not None:
            # Keep the banner body only as a fingerprint of its header.
            banner_node.children.append(Node(line))
            if banner_end in line:
                banner_end = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("!") or _VOLATILE.match(stripped):
            continue
        indent = len(line) - len(line.lstrip(" "))
        node = Node(stripped)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1].children if stack else roots).append(node)
        stack.append((indent, node))
        if indent == 0 and stripped.startswith("banner "):
            delimiter = _banner_delimiter(stripped)
            if delimiter:
                banner_end, banner_node = delimiter, node
    return roots


def _negate(text: str) -> str:
    return text[3:] if text.startswith("no ") else f"no {text}"


def _fingerprint(node: Node) -> tuple:
    return (node.text, tuple(_fingerprint(child) for child in node.children))


def _emit_tree(node: Node, out: list[str], skipped: list[str]) -> None:
    if _SENSITIVE.match(node.text):
        skipped.append(node.text)
        return
    out.append(node.text)
    for child in node.children:
        _emit_tree(child, out, skipped)


def _diff(current: list[Node], target: list[Node], out: list[str], skipped: list[str]) -> None:
    cur = {node.text: node for node in current}
    tgt = {node.text: node for node in target}

    removed = [node for node in current if node.text not in tgt]
    added = [node for node in target if node.text not in cur]
    common = [(cur[node.text], node) for node in target if node.text in cur]

    # Leaf lines first, so global settings land before the sections that may
    # depend on them. A single-value line is replaced by its new value, not
    # negated first.
    replaced = {_singleton_key(node.text) for node in added if not node.children} - {None}
    for node in removed:
        if node.children:
            continue
        if _singleton_key(node.text) in replaced:
            continue
        if _SENSITIVE.match(node.text):
            skipped.append(node.text)
        else:
            out.append(_negate(node.text))
    for node in added:
        if not node.children:
            _emit_tree(node, out, skipped)

    # New sections (VLANs, SVIs, ACLs...) exist before anything refers to
    # them; removed sections go last, once nothing refers to them any more.
    for node in added:
        if not node.children:
            continue
        if _OPAQUE_SECTIONS.match(node.text):
            skipped.append(node.text)
        else:
            _emit_tree(node, out, skipped)

    for before, after in common:
        if _fingerprint(before) == _fingerprint(after):
            continue
        if _OPAQUE_SECTIONS.match(after.text):
            skipped.append(after.text)
            continue
        inner: list[str] = []
        _diff(before.children, after.children, inner, skipped)
        if inner:
            out.append(after.text)
            out.extend(inner)

    for node in removed:
        if not node.children:
            continue
        if _OPAQUE_SECTIONS.match(node.text) or _SENSITIVE.match(node.text):
            skipped.append(node.text)
        else:
            out.append(_negate(node.text))



def restore_commands(current: str, target: str) -> RestorePlan:
    """Configuration lines that turn ``current`` into ``target`` (no wrappers)."""
    commands: list[str] = []
    skipped: list[str] = []
    _diff(parse(current), parse(target), commands, skipped)

    safe: list[str] = []
    for command in commands:
        if any(token in command for token in _FORBIDDEN_CHARS):
            skipped.append(command)
        else:
            safe.append(command)
    # A skipped line is only reported once, without its value if it is secret.
    report: list[str] = []
    for line in skipped:
        shown = line.split()[0] + " …" if _SENSITIVE.match(line) else line
        if shown not in report:
            report.append(shown)
    return RestorePlan(commands=safe, skipped=report)


def configs_match(left: str, right: str) -> bool:
    """True when the two configs differ in nothing a restore would touch."""
    return restore_commands(left, right).empty
