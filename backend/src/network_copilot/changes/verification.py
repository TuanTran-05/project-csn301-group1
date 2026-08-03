"""Frozen, bounded verification plans and safe evidence serialization."""

import re

SENSITIVE_CONFIG_READ = re.compile(r"^show\s+(?:running|startup)-config(?:\s|$)", re.I)


def is_sensitive_verification_command(command: str) -> bool:
    normalized = " ".join(str(command).strip().split())
    return bool(SENSITIVE_CONFIG_READ.match(normalized))


def build_verification_plan(assessment, requested_commands, device):
    requested = list(requested_commands or [])
    if assessment.verification_level == "semantic" and assessment.expectations:
        item = assessment.expectations[0]
        if item.family == "vlan":
            commands = ["show vlan brief"]
            return [{"id": "vlan: %s" % item.data["vlan_id"], "label": "VLAN %s" % item.data["vlan_id"], "strategy": "vlan", "commands": commands, "required": True, "sensitive": False, "expectation": item.to_dict()}]
    commands = requested or ["show running-config"]
    return [{"id": "generic:" + command.casefold().replace(" ", "-"), "label": command, "strategy": "generic", "commands": [command], "required": True, "sensitive": is_sensitive_verification_command(command)} for command in commands]


def flatten_verification_commands(plan):
    return [command for check in plan for command in check.get("commands", [])]


def serialize_verification_evidence(plan, legacy_commands, evidence):
    data = dict(evidence or {})
    sensitive_ids = {check.get("id") for check in (plan or []) if check.get("sensitive")}
    if not plan:
        sensitive_ids = {command for command in (legacy_commands or []) if is_sensitive_verification_command(command)}
    safe = {}
    for key, value in data.items():
        row = dict(value) if isinstance(value, dict) else {"output": value}
        sensitive = key in sensitive_ids or bool(row.get("redacted")) or is_sensitive_verification_command(row.get("label", key))
        if sensitive:
            row["output"] = ""
            row["redacted"] = True
            row["details"] = {"message": "Sensitive verification details omitted."}
        safe[key] = row
    return safe or None


def run_verification(change, client):
    plan = change.verification_plan or [{"id": c, "label": c, "strategy": "generic", "commands": [c], "required": True, "sensitive": is_sensitive_verification_command(c)} for c in (change.verification_commands or [])]
    cache = {}
    results = {}
    all_passed = True
    from ..parsers import parse_vlan_brief
    for check in plan:
        outputs = []
        for command in check.get("commands", []):
            if command not in cache:
                cache[command] = client.run_show(command).output
            outputs.append(cache[command])
        output = "\n".join(outputs)
        passed = bool(output.strip()) and "% Invalid input" not in output and "% Incomplete command" not in output
        details = ["Command returned output."] if passed else ["The device returned no usable output."]
        if check.get("strategy") == "vlan":
            rows = {row["vlan_id"]: row for row in parse_vlan_brief(output)}
            expected = check.get("expectation", {}).get("data", {})
            row = rows.get(expected.get("vlan_id"))
            passed = row is not None and (not expected.get("name") or row["name"] == expected["name"])
            details = ["VLAN expectation satisfied."] if passed else ["VLAN expectation was not satisfied."]
        if check.get("required", True) and not passed:
            all_passed = False
        row = {"label": check.get("label", check["id"]), "passed": passed, "required": check.get("required", True), "semantic": check.get("strategy") != "generic", "redacted": bool(check.get("sensitive")), "output": "" if check.get("sensitive") else output, "details": details}
        results[check["id"]] = row
        for command in check.get("commands", []):
            results.setdefault(command, row)
    return all_passed, results
