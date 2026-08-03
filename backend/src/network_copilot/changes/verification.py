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
        if item.family == "access_port":
            interface = item.data["interface"]
            commands = [f"show interfaces {interface} switchport", "show vlan brief"]
            return [{"id": f"access_port:{interface}", "label": f"Access port {interface}", "strategy": "access_port", "commands": commands, "required": True, "sensitive": False, "expectation": item.to_dict()}]
        if item.family == "trunk_port":
            interface = item.data["interface"]
            commands = [f"show interfaces {interface} switchport", "show interfaces trunk"]
            return [{"id": f"trunk_port:{interface}", "label": f"Trunk port {interface}", "strategy": "trunk_port", "commands": commands, "required": True, "sensitive": False, "expectation": item.to_dict()}]
        if item.family in {"interface_description", "interface_admin_state", "interface_ipv4"}:
            interface = item.data["interface"]
            command = f"show running-config interface {interface}" if item.family == "interface_description" else "show ip interface brief"
            return [{"id": f"{item.family}:{interface}", "label": f"{item.family} {interface}", "strategy": item.family, "commands": [command], "required": True, "sensitive": is_sensitive_verification_command(command), "expectation": item.to_dict()}]
        if item.family == "static_route":
            return [{"id": "route:" + str(item.data.get("network")), "label": "Static route", "strategy": "static_route", "commands": ["show ip route"], "required": True, "sensitive": False, "expectation": item.to_dict()}]
        if item.family == "save_config":
            return [{"id": "save:startup-config", "label": "Startup configuration", "strategy": "save_config", "commands": ["show startup-config"], "required": True, "sensitive": True, "expectation": item.to_dict()}]
        if item.family == "ipv4_acl":
            data=item.data; return [{"id":"acl:"+str(data["name"]),"label":"IPv4 ACL","strategy":"ipv4_acl","commands":["show access-lists",f"show running-config interface {data['interface']}"],"required":True,"sensitive":True,"expectation":item.to_dict()}]
        if item.family == "ios_dhcp_pool":
            return [
                {"id":"dhcp-config:"+str(item.data["pool"]),"label":"DHCP configuration","strategy":"ios_dhcp_pool_config","commands":["show running-config | section ^ip dhcp"],"required":True,"sensitive":True,"expectation":item.to_dict()},
                {"id":"dhcp-pool:"+str(item.data["pool"]),"label":"DHCP pool observation","strategy":"ios_dhcp_pool","commands":["show ip dhcp pool"],"required":False,"sensitive":False,"expectation":item.to_dict()},
            ]
        if item.family == "single_area_ospf":
            process = str(item.data["process_id"])
            return [
                {"id":"ospf-config:"+process,"label":"OSPF configuration","strategy":"single_area_ospf","commands":["show running-config | section ^router ospf"],"required":True,"sensitive":True,"expectation":item.to_dict()},
                {"id":"ospf-neighbors:"+process,"label":"OSPF neighbors","strategy":"generic","commands":["show ip ospf neighbor"],"required":False,"sensitive":False,"expectation":item.to_dict()},
                {"id":"ospf-routes:"+process,"label":"OSPF routes","strategy":"generic","commands":["show ip route"],"required":False,"sensitive":False,"expectation":item.to_dict()},
            ]
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
        sensitive = (not plan and bool(sensitive_ids)) or key in sensitive_ids or bool(row.get("redacted")) or is_sensitive_verification_command(row.get("label", key))
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
        elif check.get("strategy") in {"access_port", "trunk_port"}:
            from ..parsers import parse_switchport_detail, parse_interfaces_trunk, parse_vlan_brief
            expected = check.get("expectation", {}).get("data", {})
            switch = parse_switchport_detail(outputs[0])
            interface = expected.get("interface", "")
            target = next((r for r in switch if r.get("interface", "").casefold() == interface.casefold() or r.get("interface", "").casefold().endswith(interface.casefold())), None)
            if check.get("strategy") == "access_port":
                vlan_rows = {r["vlan_id"]: r for r in parse_vlan_brief(outputs[1])}
                vlan = vlan_rows.get(expected.get("access_vlan"))
                passed = bool(target and target.get("administrative_mode") == "access" and target.get("access_vlan") == expected.get("access_vlan") and vlan and interface.casefold() in str(vlan.get("ports", "")).casefold())
            else:
                trunks = parse_interfaces_trunk(outputs[1])
                trunk = next((r for r in trunks if r.get("interface", "").casefold().endswith(interface.casefold())), None)
                expected_vlans = set(expected.get("allowed_vlans", []))
                passed = bool(target and target.get("administrative_mode") == "trunk" and trunk and trunk.get("status") == "trunking" and set(target.get("allowed_vlans", [])) == expected_vlans and set(trunk.get("allowed_vlans", [])) == expected_vlans)
            details = ["Switchport expectation satisfied."] if passed else ["Switchport expectation was not satisfied."]
        elif check.get("strategy") == "interface_description":
            from ..parsers import extract_interface_stanza
            expected = check["expectation"]["data"].get("description")
            stanza = extract_interface_stanza(output, check["expectation"]["data"]["interface"])
            desired = (f" description {expected}" if expected is not None else " no description")
            passed = desired in stanza
            details = ["Interface description expectation satisfied."] if passed else ["Interface description expectation was not satisfied."]
        elif check.get("strategy") in {"interface_admin_state", "interface_ipv4"}:
            from ..parsers import parse_ip_interface_brief
            expected = check["expectation"]["data"]
            rows = parse_ip_interface_brief(output)
            target = next((r for r in rows if r.get("interface", "").casefold().endswith(expected.get("interface", "").casefold())), None)
            if check.get("strategy") == "interface_admin_state":
                actual_enabled = bool(target) and target.get("status", "").casefold() != "administratively down"
                passed = actual_enabled == bool(expected.get("enabled"))
            else:
                passed = bool(target) and (expected.get("address") in str(target.get("ip_address")))
            details = ["Interface expectation satisfied."] if passed else ["Interface expectation was not satisfied."]
        elif check.get("strategy") == "static_route":
            from ..parsers import parse_ip_routes
            expected = check["expectation"]["data"]
            rows = parse_ip_routes(output)
            passed = any(str(expected.get("network")) == str(row.get("network")) for row in rows)
            details = ["Route expectation satisfied."] if passed else ["Route expectation was not satisfied."]
        elif check.get("strategy") == "ipv4_acl":
            from ..parsers import parse_access_lists, extract_interface_stanza
            expected = check["expectation"]["data"]
            lists = parse_access_lists(outputs[0])
            acl = next((row for row in lists if row.get("name", "").casefold() == str(expected.get("name")).casefold()), None)
            stanza = extract_interface_stanza(outputs[1], expected["interface"])
            attachment = f" ip access-group {expected['name']} {expected['direction']}"
            actual_rules = []
            if acl:
                for rule in acl.get("rules", []):
                    source = rule["source"] if rule["wildcard"] is None else f"{rule['source']} {rule['wildcard']}"
                    actual_rules.append(f"{rule['action']} {source}".casefold())
            expected_rules = [str(rule).casefold() for rule in expected.get("rules", [])]
            passed = bool(acl and actual_rules == expected_rules and attachment.casefold() in [line.casefold() for line in stanza])
            details = ["Standard ACL definition and attachment satisfied."] if passed else ["ACL definition or interface attachment was not satisfied."]
        elif check.get("strategy") == "ios_dhcp_pool":
            from ..parsers import parse_ip_dhcp_pool
            expected = check["expectation"]["data"]
            rows = parse_ip_dhcp_pool(output)
            pool = next((row for row in rows if row.get("name", "").casefold() == str(expected.get("pool")).casefold()), None)
            passed = bool(pool and (not pool.get("network") or str(expected.get("network")) in str(pool.get("network"))))
            details = ["DHCP pool was observed."] if passed else ["DHCP pool expectation was not satisfied."]
        elif check.get("strategy") == "ios_dhcp_pool_config":
            expected = check["expectation"]["data"]
            lines = [line.strip().casefold() for line in output.splitlines()]
            pool_line = f"ip dhcp pool {expected['pool']}".casefold()
            network_line = f"network {expected['network']}".casefold()
            passed = pool_line in lines and (network_line in lines or any(expected.get("network", "").split("/")[0].casefold() in line for line in lines))
            details = ["DHCP configuration expectation satisfied."] if passed else ["DHCP configuration expectation was not satisfied."]
        elif check.get("strategy") == "single_area_ospf":
            expected = check["expectation"]["data"]
            lines = [line.strip().casefold() for line in output.splitlines()]
            process = f"router ospf {expected['process_id']}".casefold()
            networks_ok = all(f"network {item['address']} {item['wildcard']} area 0".casefold() in lines for item in expected.get("networks", []))
            passive_ok = all(f"passive-interface {interface}".casefold() in lines for interface in expected.get("passive_interfaces", []))
            passed = process in lines and networks_ok and passive_ok
            details = ["Single-area OSPF configuration satisfied."] if passed else ["OSPF configuration expectation was not satisfied."]
        if check.get("required", True) and not passed:
            all_passed = False
        row = {"label": check.get("label", check["id"]), "passed": passed, "required": check.get("required", True), "semantic": check.get("strategy") != "generic", "redacted": bool(check.get("sensitive")), "output": "" if check.get("sensitive") else output, "details": details}
        results[check["id"]] = row
        for command in check.get("commands", []):
            results.setdefault(command, row)
    return all_passed, results
