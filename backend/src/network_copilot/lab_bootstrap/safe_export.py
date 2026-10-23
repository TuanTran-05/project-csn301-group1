"""Create review-only ZIPs from validated PNETLab IP plans.

These exports contain planning documents only. They never contain startup
configuration and are not PNETLab lab-import archives.
"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import re
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo


MAX_EXPORT_BYTES = 5 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SAFE_PLAN_ID = re.compile(r"^[a-f0-9]{64}$")
_SENSITIVE_TEXT = re.compile(
    r"\b(password|passwd|secret|token|api[_-]?key|authorization|credential|username|"
    r"private[_ -]?key|passphrase|community|pre[-_ ]shared[-_ ]key)\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    re.I,
)


class SafeExportError(ValueError):
    """The supplied preview cannot safely be exported."""


def _text(value, label: str, limit: int = 512, *, allow_empty: bool = False) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise SafeExportError(f"The {label} contains invalid text.")
    if _SENSITIVE_TEXT.search(value):
        raise SafeExportError(f"Credential-like text in {label} cannot be exported.")
    return value


def _hostname(name: str, node_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-")[:63]
    return value or f"Node-{node_id}"


def _validate_plan(plan: dict, project_id: int) -> tuple[list[dict], list[dict], dict]:
    if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id < 1:
        raise SafeExportError("A valid project ID is required.")
    if not isinstance(plan, dict) or plan.get("status") != "PLANNED" or plan.get("ready_to_import") is not False:
        raise SafeExportError("A complete PLANNED preview is required for safe export.")

    plan_id = plan.get("plan_id")
    manifest = plan.get("manifest")
    if not isinstance(plan_id, str) or not _SAFE_PLAN_ID.fullmatch(plan_id):
        raise SafeExportError("The preview plan ID is invalid.")
    if (
        not isinstance(manifest, dict)
        or manifest.get("status") != "PLANNED"
        or plan.get("gateway_status") != "NOT_SUPPORTED"
        or manifest.get("gateway_status") != "NOT_SUPPORTED"
        or manifest.get("plan_id") != plan_id
        or manifest.get("configuration_artifacts") != []
        or manifest.get("topology_sha256") != plan.get("topology_sha256")
        or manifest.get("management_segment_id") != plan.get("management_segment_id")
    ):
        raise SafeExportError("The preview manifest is incomplete or does not match the plan.")

    management_id = plan.get("management_segment_id")
    segments = plan.get("segments")
    nodes = plan.get("nodes")
    if not isinstance(management_id, str) or not isinstance(segments, list) or not isinstance(nodes, list):
        raise SafeExportError("A selected management network and complete topology plan are required.")
    if not 1 <= len(nodes) <= 500:
        raise SafeExportError("The preview must contain between 1 and 500 nodes.")

    segment_by_id = {}
    for segment in segments:
        if not isinstance(segment, dict):
            raise SafeExportError("The preview contains an invalid segment.")
        segment_id = segment.get("id")
        kind = segment.get("kind")
        if (
            not isinstance(segment_id, str)
            or not re.fullmatch(r"(?:network:[A-Za-z0-9_-]{1,32}|cable:[0-9]{1,5})", segment_id)
            or kind not in {"network", "direct"}
            or (kind == "network") != segment_id.startswith("network:")
            or segment_id in segment_by_id
        ):
            raise SafeExportError("The preview contains an invalid or duplicate segment ID.")
        _text(segment_id, "segment ID", 64)
        subnet = segment.get("subnet")
        if subnet is not None:
            if not isinstance(subnet, str):
                raise SafeExportError("The preview contains an invalid segment subnet.")
            try:
                parsed = ipaddress.ip_network(subnet, strict=True)
            except (TypeError, ValueError) as exc:
                raise SafeExportError("The preview contains an invalid segment subnet.") from exc
            if not isinstance(parsed, ipaddress.IPv4Network):
                raise SafeExportError("The preview contains a non-IPv4 segment subnet.")
        endpoints = segment.get("endpoints")
        if not isinstance(endpoints, list):
            raise SafeExportError("The preview contains invalid segment endpoints.")
        segment_by_id[segment_id] = segment

    selected_segment = segment_by_id.get(management_id)
    if not selected_segment or selected_segment.get("kind") != "network":
        raise SafeExportError("A discovered PNETLab network must be selected as management segment.")

    seen_nodes = set()
    seen_ips = set()
    segment_endpoints = {
        segment_id: {
            (endpoint.get("node_id"), endpoint.get("interface"))
            for endpoint in segment["endpoints"]
            if isinstance(endpoint, dict)
        }
        for segment_id, segment in segment_by_id.items()
    }
    safe_nodes = []
    address_count = 0
    for node in nodes:
        if not isinstance(node, dict):
            raise SafeExportError("The preview contains an invalid node.")
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not _SAFE_ID.fullmatch(node_id) or node_id in seen_nodes:
            raise SafeExportError("The preview contains an invalid or duplicate node ID.")
        seen_nodes.add(node_id)
        name = _text(node.get("name"), "node name", 128)
        template = _text(node.get("template", ""), "template", 128, allow_empty=True)
        image = _text(node.get("image"), "image", 512, allow_empty=True)
        image_build = _text(node.get("image_build"), "image build", 128, allow_empty=True)
        addresses = node.get("addresses")
        if not isinstance(addresses, list):
            raise SafeExportError("The preview contains invalid interface assignments.")

        safe_addresses = []
        for address in addresses:
            address_count += 1
            if address_count > 10_000 or not isinstance(address, dict):
                raise SafeExportError("The preview contains too many or invalid interface assignments.")
            interface = _text(address.get("interface"), "interface name", 64)
            segment_id = address.get("segment_id")
            if not isinstance(segment_id, str) or segment_id not in segment_by_id:
                raise SafeExportError("An interface assignment references an unknown segment.")
            ip = address.get("ip")
            prefix = address.get("prefix_length")
            subnet = address.get("subnet")
            if not isinstance(ip, str) or not isinstance(subnet, str):
                raise SafeExportError("An interface assignment has invalid IPv4 data.")
            if isinstance(prefix, bool) or not isinstance(prefix, int) or not 0 <= prefix <= 32:
                raise SafeExportError("An interface assignment has an invalid IPv4 prefix.")
            try:
                parsed_ip = ipaddress.ip_address(ip)
                parsed_subnet = ipaddress.ip_network(subnet, strict=True)
            except (TypeError, ValueError) as exc:
                raise SafeExportError("An interface assignment has invalid IPv4 data.") from exc
            if (
                not isinstance(parsed_ip, ipaddress.IPv4Address)
                or not isinstance(parsed_subnet, ipaddress.IPv4Network)
                or parsed_subnet.prefixlen != prefix
                or parsed_ip not in parsed_subnet
                or ip in seen_ips
                or segment_by_id[segment_id].get("subnet") != str(parsed_subnet)
                or (node_id, interface) not in segment_endpoints[segment_id]
            ):
                raise SafeExportError("An interface assignment is inconsistent or duplicates an IP.")
            seen_ips.add(ip)
            safe_addresses.append({
                "interface": interface,
                "ip": str(parsed_ip),
                "prefix_length": prefix,
                "subnet": str(parsed_subnet),
                "segment_id": segment_id,
                "segment_kind": segment_by_id[segment_id]["kind"],
                "addressing": _text(address.get("addressing", ""), "addressing type", 32, allow_empty=True),
            })

        safe_nodes.append({
            "node_id": node_id,
            "name": name,
            "hostname": _hostname(name, node_id),
            "template": template or None,
            "image": image or None,
            "image_build": image_build or None,
            "interfaces": safe_addresses,
            "configuration_status": "NEEDS_MANUAL_ACTION",
            "configuration_reason": (
                "This preview ZIP contains no startup configuration; exact image/build profile and CLI behavior remain unverified."
            ),
            "ssh_status": "NEEDS_MANUAL_ACTION",
            "ssh_reason": "No SSH setup or connection was attempted or verified.",
        })

    safe_segments = []
    for segment_id, segment in segment_by_id.items():
        safe_endpoints = []
        for endpoint in segment["endpoints"]:
            if not isinstance(endpoint, dict):
                raise SafeExportError("The preview contains an invalid segment endpoint.")
            endpoint_node = endpoint.get("node_id")
            if not isinstance(endpoint_node, str) or endpoint_node not in seen_nodes:
                raise SafeExportError("A segment endpoint references an unknown node.")
            safe_endpoints.append({
                "node_id": endpoint_node,
                "interface": _text(endpoint.get("interface"), "endpoint interface", 64, allow_empty=True),
            })
        safe_segments.append({
            "id": segment_id,
            "kind": segment["kind"],
            "subnet": segment.get("subnet"),
            "endpoints": safe_endpoints,
        })

    warnings = plan.get("warnings", [])
    if not isinstance(warnings, list):
        raise SafeExportError("The preview warnings are invalid.")
    topology_sha256 = plan.get("topology_sha256")
    if not isinstance(topology_sha256, str) or not _SAFE_PLAN_ID.fullmatch(topology_sha256):
        raise SafeExportError("The topology fingerprint is invalid.")
    return safe_nodes, safe_segments, {
        "plan_id": plan_id,
        "topology_sha256": topology_sha256,
        "management_segment_id": management_id,
        "management_pool": _text(plan.get("management_pool"), "management pool", 43),
        "data_pool": _text(plan.get("data_pool"), "data pool", 43),
        "warnings": [_text(warning, "plan warning", 512) for warning in warnings],
    }


def _csv_safe(value) -> str:
    text = str(value)
    if text[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + text
    return text


def _ip_csv(nodes: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("node_id", "hostname", "interface", "ip", "prefix_length", "subnet", "segment_id", "segment_kind", "addressing"))
    for node in nodes:
        for address in node["interfaces"]:
            writer.writerow(tuple(_csv_safe(value) for value in (
                node["node_id"], node["hostname"], address["interface"], address["ip"],
                address["prefix_length"], address["subnet"], address["segment_id"],
                address["segment_kind"], address["addressing"],
            )))
    return output.getvalue().encode("utf-8")


def build_safe_export_package(plan: dict, project_id: int) -> bytes:
    """Build a review ZIP with no topology file or device configuration."""
    nodes, segments, metadata = _validate_plan(plan, project_id)
    extra_warnings = []
    if any(not node["image"] or not node["image_build"] for node in nodes):
        extra_warnings.append("At least one node is missing image or exact build metadata.")
    manifest = {
        "schema_version": "1.0",
        "package_type": "pnetlab-ip-plan-preview",
        "status": "PLANNED",
        "gateway_status": "NOT_SUPPORTED",
        "applied": False,
        "verified": False,
        "project_id": project_id,
        "plan_id": metadata["plan_id"],
        "topology_sha256": metadata["topology_sha256"],
        "management_segment_id": metadata["management_segment_id"],
        "management_pool": metadata["management_pool"],
        "data_pool": metadata["data_pool"],
        "topology_included": False,
        "topology_file": None,
        "nodes": nodes,
        "segments": segments,
        "configuration_artifacts": [],
        "warnings": list(dict.fromkeys([
            *metadata["warnings"],
            *extra_warnings,
            "This ZIP is a planning preview only; it has not been applied or verified.",
            "No source .unl is attached to this preview export; archive analysis is separate and does not prove topology equivalence.",
            "No startup configuration, SSH credentials, or device changes are included.",
            "Gateway addresses are not supported by this planner and have not been validated or assigned.",
            "The selected management network's reachability has not been verified.",
        ])),
    }
    entries = {
        "README.txt": (
            "PNETLab IP plan preview package\n\n"
            f"Project ID: {project_id}\nPlan ID: {metadata['plan_id']}\n"
            "Status: PLANNED (not applied, imported, configured, or verified)\n\n"
            "Gateway status: NOT_SUPPORTED (this planner does not accept or assign gateway addresses).\n\n"
            "Files in this ZIP:\n"
            "- manifest.json: validated topology and planning metadata\n"
            "- ip-plan.csv: proposed interface addresses and segment types\n"
            "- checksums.txt: SHA-256 checksums for the other package files\n\n"
            "No source .unl is attached to this preview export; this ZIP contains no topology file.\n"
            "A separately analyzed source archive is not attached because its mapping to this IP plan has not been verified.\n"
            "This is a review/export package, not a PNETLab lab-import archive. Do not use it as a lab import.\n"
            "It includes no startup-config, credentials, SSH setup, or device changes.\n"
            "Before using the proposed addresses, verify the selected management network, existing device configuration, routing, and image-specific requirements manually.\n"
        ).encode("utf-8"),
        "manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        "ip-plan.csv": _ip_csv(nodes),
    }
    checksums = "".join(
        f"{hashlib.sha256(entries[path]).hexdigest()}  {path}\n"
        for path in sorted(entries)
    ).encode("ascii")
    entries["checksums.txt"] = checksums
    total_size = sum(map(len, entries.values()))
    if total_size > MAX_EXPORT_BYTES:
        raise SafeExportError("The preview export exceeds the ZIP size limit.")

    target = io.BytesIO()
    try:
        with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for path, content in sorted(entries.items()):
                info = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.create_system = 0
                archive.writestr(info, content)
    except Exception as exc:
        raise SafeExportError("The preview ZIP could not be created.") from exc
    result = target.getvalue()
    if len(result) > MAX_EXPORT_BYTES:
        raise SafeExportError("The preview export exceeds the ZIP size limit.")
    return result


def analyze_safe_export_package(archive_bytes: bytes) -> dict:
    """Validate this application's review-only ZIP format without extracting it.

    This is separate from the PNETLab .unl parser. A valid result confirms
    package structure and checksums only; it never establishes import
    compatibility or makes the package eligible for planning/application.
    """
    if not isinstance(archive_bytes, bytes) or not archive_bytes:
        raise SafeExportError("The preview ZIP is empty.")
    if len(archive_bytes) > MAX_EXPORT_BYTES:
        raise SafeExportError("The preview ZIP exceeds the size limit.")

    expected_names = {"README.txt", "checksums.txt", "ip-plan.csv", "manifest.json"}
    try:
        with ZipFile(io.BytesIO(archive_bytes), "r") as archive:
            infos = archive.infolist()
            if len(infos) > 8:
                raise SafeExportError("The preview ZIP contains too many entries.")
            names = set()
            normalized_names = set()
            total_size = 0
            for info in infos:
                name = info.filename
                if (
                    not name or len(name) > 128 or "/" in name or "\\" in name
                    or name in {".", ".."}
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                ):
                    raise SafeExportError("The preview ZIP contains an unsafe member name.")
                normalized = name.casefold()
                if normalized in normalized_names:
                    raise SafeExportError("The preview ZIP contains duplicate filenames.")
                normalized_names.add(normalized)
                names.add(name)
                if info.is_dir() or info.flag_bits & 0x1:
                    raise SafeExportError("Directories and encrypted preview ZIP entries are not supported.")
                if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                    raise SafeExportError("The preview ZIP uses an unsupported compression method.")
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and (mode & 0o170000) == 0o120000:
                    raise SafeExportError("Symbolic links are not supported in preview ZIPs.")
                if info.file_size < 0 or info.file_size > MAX_EXPORT_BYTES:
                    raise SafeExportError("A preview ZIP member exceeds the size limit.")
                total_size += info.file_size
                if total_size > MAX_EXPORT_BYTES:
                    raise SafeExportError("The preview ZIP expands beyond the size limit.")
            if names != expected_names:
                raise SafeExportError("The preview ZIP must contain exactly the four preview package files.")
            if archive.testzip() is not None:
                raise SafeExportError("The preview ZIP contains a corrupt member or CRC mismatch.")
            entries = {name: archive.read(name) for name in expected_names}
    except SafeExportError:
        raise
    except (BadZipFile, OSError, RuntimeError, ValueError, EOFError) as exc:
        raise SafeExportError("The preview ZIP is malformed or cannot be read.") from exc

    try:
        checksum_text = entries["checksums.txt"].decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise SafeExportError("The preview ZIP checksum list is not ASCII text.") from exc
    checksum_rows = checksum_text.splitlines()
    if len(checksum_rows) != len(expected_names) - 1:
        raise SafeExportError("The preview ZIP checksum list is incomplete or invalid.")
    checksums = {}
    for row in checksum_rows:
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9._-]{1,64})", row)
        if not match or match.group(2) in checksums:
            raise SafeExportError("The preview ZIP checksum list is malformed.")
        checksums[match.group(2)] = match.group(1)
    data_names = expected_names - {"checksums.txt"}
    if set(checksums) != data_names or any(
        hashlib.sha256(entries[name]).hexdigest() != checksums[name] for name in data_names
    ):
        raise SafeExportError("A preview ZIP checksum does not match its file contents.")

    try:
        manifest = json.loads(entries["manifest.json"].decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafeExportError("The preview ZIP manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(manifest, dict):
        raise SafeExportError("The preview ZIP manifest must be a JSON object.")
    if manifest.get("schema_version") != "1.0" or manifest.get("package_type") != "pnetlab-ip-plan-preview":
        raise SafeExportError("The preview ZIP schema or package type is not supported.")
    if (
        manifest.get("status") != "PLANNED"
        or manifest.get("gateway_status") != "NOT_SUPPORTED"
        or manifest.get("applied") is not False
        or manifest.get("verified") is not False
        or manifest.get("topology_included") is not False
        or manifest.get("topology_file") is not None
        or manifest.get("configuration_artifacts") != []
    ):
        raise SafeExportError("The preview ZIP manifest has an unsupported or unsafe state.")
    project_id = manifest.get("project_id")
    if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id < 1:
        raise SafeExportError("The preview ZIP manifest has an invalid project ID.")
    warnings = manifest.get("warnings")
    if not isinstance(warnings, list) or len(warnings) > 1000 or any(not isinstance(item, str) for item in warnings):
        raise SafeExportError("The preview ZIP manifest warnings are invalid.")

    auto_warnings = {
        "This ZIP is a planning preview only; it has not been applied or verified.",
        "No source .unl is attached to this preview export; archive analysis is separate and does not prove topology equivalence.",
        "No startup configuration, SSH credentials, or device changes are included.",
        "Gateway addresses are not supported by this planner and have not been validated or assigned.",
        "The selected management network's reachability has not been verified.",
        "At least one node is missing image or exact build metadata.",
    }
    plan_id = manifest.get("plan_id")
    topology_sha256 = manifest.get("topology_sha256")
    management_segment_id = manifest.get("management_segment_id")
    nodes = manifest.get("nodes")
    segments = manifest.get("segments")
    if not isinstance(nodes, list) or not isinstance(segments, list):
        raise SafeExportError("The preview ZIP manifest is missing plan topology data.")
    base_warnings = [warning for warning in warnings if warning not in auto_warnings]
    validation_plan = {
        "status": "PLANNED",
        "ready_to_import": False,
        "plan_id": plan_id,
        "topology_sha256": topology_sha256,
        "management_segment_id": management_segment_id,
        "gateway_status": "NOT_SUPPORTED",
        "management_pool": manifest.get("management_pool"),
        "data_pool": manifest.get("data_pool"),
        "segments": segments,
        "nodes": [],
        "warnings": base_warnings,
        "manifest": {
            "status": "PLANNED",
            "plan_id": plan_id,
            "topology_sha256": topology_sha256,
            "management_segment_id": management_segment_id,
            "gateway_status": "NOT_SUPPORTED",
            "configuration_artifacts": [],
        },
    }
    for node in nodes:
        if not isinstance(node, dict) or "interfaces" not in node:
            raise SafeExportError("The preview ZIP contains an invalid node record.")
        validation_plan["nodes"].append({
            "node_id": node.get("node_id"),
            "name": node.get("name"),
            "template": node.get("template") or "",
            "image": node.get("image") or "",
            "image_build": node.get("image_build") or "",
            "addresses": node.get("interfaces"),
        })
    safe_nodes, safe_segments, metadata = _validate_plan(validation_plan, project_id)

    # Rebuild the canonical artifact to ensure no unmodeled data (including
    # credential-like fields) is accepted or echoed back by this parser.
    canonical_bytes = build_safe_export_package(validation_plan, project_id)
    try:
        with ZipFile(io.BytesIO(canonical_bytes), "r") as canonical:
            canonical_entries = {name: canonical.read(name) for name in expected_names}
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise SafeExportError("The preview package could not be validated canonically.") from exc
    if entries != canonical_entries:
        raise SafeExportError("The preview ZIP files do not match the validated manifest and plan.")

    return {
        "status": "ANALYZED_UNVERIFIED_EXPORT",
        "preview_compatible": False,
        "import_compatible": False,
        "checksums_verified": True,
        "files": sorted(expected_names),
        "manifest": manifest,
        "inspection": {
            "status": "INSPECTION_ONLY",
            "planner_eligible": False,
            "blockers": [
                "This is a generated IP-plan preview package, not a verified PNETLab source topology.",
                "Checksums verify the exported bytes only; they do not prove PNETLab import compatibility.",
            ],
            "issues": list(metadata["warnings"]),
            "segments": [
                {
                    "id": segment["id"],
                    "kind": segment["kind"],
                    "subnet": segment.get("subnet"),
                    "endpoints": segment["endpoints"],
                }
                for segment in safe_segments
            ],
        },
    }


def build_safe_inspection_report_package(analysis: dict, inspection: dict) -> bytes:
    """Build a summary-only report for an accepted, unverified archive inspection.

    This artifact is distinct from an IP-plan preview. It includes hashes and
    record counts only; it never includes source XML, topology records, labels,
    IP assignments, or configuration. It cannot be imported into PNETLab.
    """
    if (
        not isinstance(analysis, dict)
        or analysis.get("status") != "ANALYZED_UNVERIFIED_SCHEMA"
        or analysis.get("preview_compatible") is not False
        or not isinstance(inspection, dict)
        or inspection.get("status") != "INSPECTION_ONLY"
        or inspection.get("planner_eligible") is not False
    ):
        raise SafeExportError("Only an unverified inspection-only result can be exported.")

    nodes = analysis.get("nodes")
    networks = analysis.get("networks")
    links = analysis.get("links")
    interface_networks = analysis.get("interface_networks")
    warnings = analysis.get("warnings")
    segments = inspection.get("segments")
    blockers = inspection.get("blockers")
    issues = inspection.get("issues")
    collections = (nodes, networks, links, interface_networks, warnings, segments, blockers, issues)
    if any(not isinstance(items, list) or len(items) > 20_000 for items in collections):
        raise SafeExportError("The inspection result is incomplete or exceeds report limits.")
    if not nodes or issues:
        raise SafeExportError("Inspection issues prevent exporting a review report.")
    if any(not isinstance(item, str) for item in (*warnings, *blockers, *issues)):
        raise SafeExportError("The inspection result contains invalid validation messages.")

    interface_count = 0
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("interfaces"), list):
            raise SafeExportError("The inspection result contains invalid node metadata.")
        interface_count += len(node["interfaces"])
        if interface_count > 20_000 or any(not isinstance(item, dict) for item in node["interfaces"]):
            raise SafeExportError("The inspection result contains invalid interface metadata.")
    if any(not isinstance(network, dict) for network in networks):
        raise SafeExportError("The inspection result contains invalid network metadata.")
    if any(not isinstance(link, dict) or link.get("kind") not in {"direct", "network"} for link in links):
        raise SafeExportError("The inspection result contains invalid link metadata.")
    if any(not isinstance(mapping, dict) for mapping in interface_networks):
        raise SafeExportError("The inspection result contains invalid attachment metadata.")
    if any(not isinstance(segment, dict) or segment.get("kind") not in {"direct", "network"} for segment in segments):
        raise SafeExportError("The inspection result contains invalid segment metadata.")

    archive_sha256 = analysis.get("source_archive_sha256")
    unl_sha256 = analysis.get("source_unl_sha256")
    if (
        not isinstance(archive_sha256, str)
        or not _SAFE_PLAN_ID.fullmatch(archive_sha256)
        or not isinstance(unl_sha256, str)
        or not _SAFE_PLAN_ID.fullmatch(unl_sha256)
    ):
        raise SafeExportError("The inspection result is missing valid source checksums.")

    manifest = {
        "schema_version": "1.0",
        "package_type": "pnetlab-topology-inspection-summary",
        "archive_status": "ANALYZED_UNVERIFIED_SCHEMA",
        "preview_compatible": False,
        "import_compatible": False,
        "planner_status": "NOT_RUN",
        "planner_eligible": False,
        "gateway_status": "NOT_SUPPORTED",
        "device_actions": "NOT_PERFORMED",
        "applied": False,
        "verified": False,
        "source_archive_included": False,
        "topology_file": None,
        "configuration_artifacts": [],
        "source_archive_sha256": archive_sha256,
        "source_unl_sha256": unl_sha256,
        "counts": {
            "nodes": len(nodes),
            "interfaces": interface_count,
            "networks": len(networks),
            "direct_links": sum(link["kind"] == "direct" for link in links),
            "network_links": sum(link["kind"] == "network" for link in links),
            "interface_network_attachments": len(interface_networks),
            "inspected_segments": len(segments),
            "warnings": len(warnings),
            "blockers": len(blockers),
            "issues": len(issues),
        },
    }
    entries = {
        "README.txt": (
            "PNETLab topology inspection summary\n\n"
            "This is a Safe Demo review report only. It is not an IP plan and it is not a PNETLab import archive.\n"
            "The source archive and XML, topology labels, IP assignments, startup configuration, and device commands are not included.\n"
            "The schema remains unverified, planner status is NOT_RUN, gateway status is NOT_SUPPORTED, and no device action was performed.\n"
            "manifest.json contains source hashes and counts of inspected records and validation messages.\n"
            "Checksums cover README.txt and manifest.json only. They prove file integrity, not topology compatibility.\n"
        ).encode("utf-8"),
        "manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    entries["checksums.txt"] = "".join(
        f"{hashlib.sha256(entries[name]).hexdigest()}  {name}\n"
        for name in sorted(entries)
    ).encode("ascii")
    if sum(len(value) for value in entries.values()) > MAX_EXPORT_BYTES:
        raise SafeExportError("The inspection report exceeds the export size limit.")

    target = io.BytesIO()
    try:
        with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for name, content in sorted(entries.items()):
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.create_system = 0
                archive.writestr(info, content)
    except Exception as exc:
        raise SafeExportError("The inspection report could not be created.") from exc
    result = target.getvalue()
    if len(result) > MAX_EXPORT_BYTES:
        raise SafeExportError("The inspection report exceeds the export size limit.")
    return result
