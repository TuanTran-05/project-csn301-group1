"""Safe, analysis-only reader for PNETLab-style ZIP exports.

The parser intentionally accepts one narrow XML layout. It never extracts
archive members and it does not adapt its result to the IP planner. Its
supported subset has been checked against one real export; broader format
compatibility remains unverified.
"""

from __future__ import annotations

import hashlib
import io
import ntpath
import re
import stat
import struct
import zipfile
import zlib
from xml.etree import ElementTree


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 256
MAX_TOTAL_UNCOMPRESSED_BYTES = 12 * 1024 * 1024
MAX_MEMBER_UNCOMPRESSED_BYTES = 4 * 1024 * 1024
MAX_UNL_BYTES = 2 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_XML_ELEMENTS = 20_000
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9 ._-]{0,123}\.zip$", re.I)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_IFACE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_LABEL = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_EOCD_STRUCT = struct.Struct("<4s4H2LH")
_CENTRAL_DIRECTORY_HEADER = struct.Struct("<4s6H3L5H2L")
_MAX_EOCD_SEARCH_BYTES = _EOCD_STRUCT.size + 0xFFFF


class PnetlabArchiveError(ValueError):
    """The uploaded archive cannot be safely analyzed."""


class UnsupportedPnetlabSchemaError(PnetlabArchiveError):
    """The ZIP is readable, but its XML structure is outside the parsed schema."""


class IncompletePnetlabTopologyError(PnetlabArchiveError):
    """The XML has a supported outer structure but lacks required topology data."""


def _validate_upload_filename(filename: str | None) -> str:
    if (
        not isinstance(filename, str)
        or not _SAFE_FILENAME.fullmatch(filename)
        or ".." in filename
        or filename in {".", ".."}
        or ntpath.basename(filename) != filename
    ):
        raise PnetlabArchiveError("Choose a ZIP file with a simple filename.")
    return filename


def _safe_member_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 512
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or name.startswith("/")
        or ntpath.splitdrive(name)[0]
    ):
        raise PnetlabArchiveError("The ZIP contains an unsafe member path.")
    candidate = name[:-1] if name.endswith("/") else name
    parts = candidate.split("/")
    if not candidate or any(part in {"", ".", ".."} for part in parts):
        raise PnetlabArchiveError("The ZIP contains an unsafe member path.")
    if any(":" in part for part in parts):
        raise PnetlabArchiveError("The ZIP contains an unsafe member path.")
    return "/".join(parts)


def _preflight_central_directory(archive_bytes: bytes) -> None:
    """Bound central-directory records before ``ZipFile`` materializes them.

    This is deliberately a narrow structural check, not a replacement ZIP
    parser. The standard library remains responsible for parsing entries,
    decompression and CRC validation after the directory count and boundaries
    have been bounded here.
    """
    window_start = max(0, len(archive_bytes) - _MAX_EOCD_SEARCH_BYTES)
    eocd_offset = archive_bytes.rfind(_EOCD_SIGNATURE, window_start)
    if eocd_offset < 0 or eocd_offset + _EOCD_STRUCT.size > len(archive_bytes):
        raise PnetlabArchiveError("The uploaded file is not a valid, readable ZIP archive.")

    (
        _signature,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        total_entries,
        central_directory_size,
        central_directory_offset,
        comment_length,
    ) = _EOCD_STRUCT.unpack_from(archive_bytes, eocd_offset)
    if eocd_offset + _EOCD_STRUCT.size + comment_length != len(archive_bytes):
        raise PnetlabArchiveError("The uploaded file is not a valid, readable ZIP archive.")
    if (
        total_entries == 0xFFFF
        or central_directory_size == 0xFFFFFFFF
        or central_directory_offset == 0xFFFFFFFF
        or (
            eocd_offset >= 20
            and archive_bytes[eocd_offset - 20:eocd_offset - 16] == _ZIP64_LOCATOR_SIGNATURE
        )
    ):
        raise PnetlabArchiveError("ZIP64 archives are not supported by this Safe Demo.")
    if disk_number != 0 or central_directory_disk != 0 or entries_on_disk != total_entries:
        raise PnetlabArchiveError("Multi-disk ZIP archives are not supported.")
    if total_entries == 0:
        raise PnetlabArchiveError("The ZIP has no files.")
    if total_entries > MAX_ARCHIVE_ENTRIES:
        raise PnetlabArchiveError("The ZIP contains too many entries.")

    central_directory_end = eocd_offset
    central_directory_start = central_directory_end - central_directory_size
    archive_prefix_size = central_directory_start - central_directory_offset
    if (
        central_directory_size > len(archive_bytes)
        or central_directory_start < 0
        or archive_prefix_size < 0
    ):
        raise PnetlabArchiveError("The ZIP has invalid central-directory boundaries.")

    cursor = central_directory_start
    actual_entries = 0
    while cursor < central_directory_end:
        if cursor + _CENTRAL_DIRECTORY_HEADER.size > central_directory_end:
            raise PnetlabArchiveError("The ZIP has a truncated central-directory entry.")
        fields = _CENTRAL_DIRECTORY_HEADER.unpack_from(archive_bytes, cursor)
        if fields[0] != _CENTRAL_DIRECTORY_SIGNATURE:
            raise PnetlabArchiveError("The ZIP has a malformed central-directory entry.")
        filename_length, extra_length, entry_comment_length = fields[10:13]
        next_cursor = (
            cursor
            + _CENTRAL_DIRECTORY_HEADER.size
            + filename_length
            + extra_length
            + entry_comment_length
        )
        if next_cursor > central_directory_end:
            raise PnetlabArchiveError("The ZIP has a truncated central-directory entry.")
        if fields[13] != 0:
            raise PnetlabArchiveError("Multi-disk ZIP entries are not supported.")
        actual_entries += 1
        if actual_entries > MAX_ARCHIVE_ENTRIES:
            raise PnetlabArchiveError("The ZIP contains too many entries.")
        cursor = next_cursor

    if cursor != central_directory_end or actual_entries != total_entries:
        raise PnetlabArchiveError("The ZIP central-directory entry count is inconsistent.")


def _xml_text(value: str | None, label: str, limit: int = 256, *, required: bool = False):
    if value is None:
        if required:
            raise PnetlabArchiveError(f"The .unl file is missing {label}.")
        return None
    if len(value) > limit or any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise PnetlabArchiveError(f"The .unl file contains an invalid {label}.")
    cleaned = value.strip()
    if required and not cleaned:
        raise PnetlabArchiveError(f"The .unl file is missing {label}.")
    return cleaned or None


def _unique_id(value: str | None, label: str) -> str:
    result = _xml_text(value, label, 64, required=True)
    if not _SAFE_ID.fullmatch(result):
        raise PnetlabArchiveError(f"The .unl file contains an invalid {label}.")
    return result


def _children(parent, tag: str):
    return [child for child in list(parent) if child.tag == tag]


def _warn_unmodeled_attributes(element, supported: set[str], label: str, warnings: list[str]):
    if set(element.attrib).difference(supported):
        warnings.append(f"{label} has additional metadata omitted from this analysis.")


def _warn_unmodeled_children(element, supported: set[str], label: str, warnings: list[str]):
    if any(child.tag not in supported for child in list(element)):
        warnings.append(f"{label} has unsupported child elements omitted from this analysis.")


def _parse_unl(data: bytes, member_name: str) -> dict:
    if not data or len(data) > MAX_UNL_BYTES:
        raise PnetlabArchiveError("The .unl file is empty or exceeds the size limit.")
    try:
        xml_text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise PnetlabArchiveError("The .unl file must be UTF-8 XML.") from exc
    upper_xml = xml_text.upper()
    if "<!DOCTYPE" in upper_xml or "<!ENTITY" in upper_xml:
        raise PnetlabArchiveError("DTD and entity declarations are not allowed in .unl files.")
    if xml_text.count("<") > MAX_XML_ELEMENTS:
        raise PnetlabArchiveError("The .unl file contains too many XML elements.")
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise PnetlabArchiveError("The .unl file contains malformed XML.") from exc

    elements = list(root.iter())
    if len(elements) > MAX_XML_ELEMENTS:
        raise PnetlabArchiveError("The .unl file contains too many XML elements.")
    if root.tag != "lab":
        raise UnsupportedPnetlabSchemaError("The .unl XML root must be <lab>.")
    topology_elements = _children(root, "topology")
    if len(topology_elements) != 1:
        raise UnsupportedPnetlabSchemaError("The .unl file must contain one <topology> element.")
    topology = topology_elements[0]
    node_containers = _children(topology, "nodes")
    network_containers = _children(topology, "networks")
    link_containers = _children(topology, "links")
    if len(node_containers) != 1 or len(network_containers) != 1 or len(link_containers) > 1:
        raise UnsupportedPnetlabSchemaError(
            "Unsupported .unl structure; expected topology/nodes and networks sections, with at most one links section."
        )

    warnings = []
    _warn_unmodeled_children(root, {"topology", "objects"}, "Lab structure", warnings)
    _warn_unmodeled_children(topology, {"nodes", "networks", "links"}, "Topology structure", warnings)
    _warn_unmodeled_children(node_containers[0], {"node"}, "Node collection", warnings)
    _warn_unmodeled_children(network_containers[0], {"network"}, "Network collection", warnings)
    if link_containers:
        _warn_unmodeled_children(link_containers[0], {"link"}, "Link collection", warnings)

    nodes = []
    node_by_id = {}
    _warn_unmodeled_attributes(root, {"name"}, "Lab", warnings)
    _warn_unmodeled_attributes(topology, set(), "Topology", warnings)
    for raw_node in _children(node_containers[0], "node"):
        _warn_unmodeled_children(raw_node, {"interface"}, "Node metadata", warnings)
        _warn_unmodeled_attributes(
            raw_node,
            {"id", "name", "type", "template", "image", "image_build", "qemu_version"},
            "Node metadata",
            warnings,
        )
        node_id = _unique_id(raw_node.get("id"), "node ID")
        if node_id in node_by_id:
            raise PnetlabArchiveError("The .unl file contains duplicate node IDs.")
        node_name = _xml_text(raw_node.get("name"), "node name", 128, required=True)
        template = _xml_text(raw_node.get("template"), "node template", 128)
        image = _xml_text(raw_node.get("image"), "node image", 512)
        image_build = _xml_text(raw_node.get("image_build"), "explicit image build", 128)
        qemu_version = _xml_text(raw_node.get("qemu_version"), "QEMU version", 64)
        interfaces = []
        interface_ids = set()
        for raw_interface in _children(raw_node, "interface"):
            _warn_unmodeled_children(raw_interface, set(), "Interface metadata", warnings)
            _warn_unmodeled_attributes(
                raw_interface,
                {"id", "name", "type", "network_id", "label"},
                "Interface metadata",
                warnings,
            )
            interface_id = _unique_id(raw_interface.get("id"), "interface ID")
            if interface_id in interface_ids:
                raise PnetlabArchiveError("A node contains duplicate interface IDs.")
            interface_ids.add(interface_id)
            interface_name = _xml_text(raw_interface.get("name"), "interface name", 128)
            interface_type = _xml_text(raw_interface.get("type"), "interface type", 64)
            network_id = _xml_text(raw_interface.get("network_id"), "interface network ID", 64)
            if network_id and not _SAFE_ID.fullmatch(network_id):
                raise PnetlabArchiveError("The .unl file contains an invalid interface network ID.")
            raw_label = raw_interface.get("label")
            if raw_label is not None:
                _xml_text(raw_label, "interface label", 128)
            if interface_name is None:
                warnings.append(f"Node {node_id} interface {interface_id} has no interface name.")
            interface = {"id": interface_id, "name": interface_name}
            if interface_type is not None:
                interface["type"] = interface_type
            if network_id is not None:
                interface["network_id"] = network_id
            if raw_label is not None:
                interface["label"] = raw_label
            interfaces.append(interface)
        if not template:
            warnings.append(f"Node {node_id} has no template metadata.")
        if not image:
            warnings.append(f"Node {node_id} has no image metadata.")
        if not image_build:
            warnings.append(f"Node {node_id} has no explicit image-build metadata.")
        node = {
            "id": node_id,
            "name": node_name,
            "type": _xml_text(raw_node.get("type"), "node type", 64),
            "template": template,
            "image": image,
            "image_build": image_build,
            "qemu_version": qemu_version,
            "interfaces": interfaces,
        }
        nodes.append(node)
        node_by_id[node_id] = node

    if not nodes:
        raise IncompletePnetlabTopologyError("The .unl topology does not contain any nodes.")

    networks = []
    network_by_id = {}
    for raw_network in _children(network_containers[0], "network"):
        _warn_unmodeled_children(raw_network, set(), "Network metadata", warnings)
        _warn_unmodeled_attributes(
            raw_network, {"id", "name", "type"}, "Network metadata", warnings
        )
        network_id = _unique_id(raw_network.get("id"), "network ID")
        if network_id in network_by_id:
            raise PnetlabArchiveError("The .unl file contains duplicate network IDs.")
        network = {
            "id": network_id,
            "name": _xml_text(raw_network.get("name"), "network name", 128),
            "type": _xml_text(raw_network.get("type"), "network type", 64),
        }
        networks.append(network)
        network_by_id[network_id] = network

    interface_networks = []
    for node in nodes:
        for interface in node["interfaces"]:
            network_id = interface.get("network_id")
            if network_id is None:
                continue
            network = network_by_id.get(network_id)
            if network is None:
                raise PnetlabArchiveError("A node interface references an unknown network.")
            interface_networks.append({
                "node_id": node["id"],
                "interface_id": interface["id"],
                "interface": interface["name"],
                "network_id": network_id,
                "network_type": network["type"],
            })

    links = []
    link_ids = set()
    for raw_link in _children(link_containers[0], "link") if link_containers else []:
        _warn_unmodeled_children(raw_link, {"interface1", "interface2"}, "Link metadata", warnings)
        _warn_unmodeled_attributes(raw_link, {"id", "type"}, "Link metadata", warnings)
        link_id = _xml_text(raw_link.get("id"), "link ID", 64)
        if link_id:
            if not _SAFE_IFACE_ID.fullmatch(link_id) or link_id in link_ids:
                raise PnetlabArchiveError("The .unl file contains an invalid or duplicate link ID.")
            link_ids.add(link_id)
        raw_endpoints = [_children(raw_link, "interface1"), _children(raw_link, "interface2")]
        if any(len(items) != 1 for items in raw_endpoints):
            raise UnsupportedPnetlabSchemaError(
                "Unsupported .unl link; each link must declare interface1 and interface2."
            )
        endpoints = []
        for raw_endpoint in (raw_endpoints[0][0], raw_endpoints[1][0]):
            _warn_unmodeled_children(raw_endpoint, set(), "Link endpoint metadata", warnings)
            _warn_unmodeled_attributes(
                raw_endpoint,
                {"node", "network", "id", "name", "interface", "label"},
                "Link endpoint metadata",
                warnings,
            )
            has_node_ref = "node" in raw_endpoint.attrib
            has_network_ref = "network" in raw_endpoint.attrib
            if has_node_ref == has_network_ref:
                raise UnsupportedPnetlabSchemaError(
                    "A .unl link endpoint must reference one node or network."
                )
            if has_node_ref:
                endpoint_id = _unique_id(raw_endpoint.get("node"), "link node reference")
                if endpoint_id not in node_by_id:
                    raise PnetlabArchiveError("A .unl link references an unknown node.")
                endpoint_kind = "node"
            else:
                endpoint_id = _unique_id(raw_endpoint.get("network"), "link network reference")
                if endpoint_id not in network_by_id:
                    raise PnetlabArchiveError("A .unl link references an unknown network.")
                endpoint_kind = "network"
            interface_id = _xml_text(raw_endpoint.get("id"), "link interface ID", 64)
            if interface_id and not _SAFE_IFACE_ID.fullmatch(interface_id):
                raise PnetlabArchiveError("The .unl file contains an invalid link interface ID.")
            endpoint_name = _xml_text(raw_endpoint.get("name"), "link interface name", 128)
            endpoint_interface = _xml_text(
                raw_endpoint.get("interface"), "link interface name", 128
            )
            if endpoint_name and endpoint_interface and endpoint_name != endpoint_interface:
                raise PnetlabArchiveError("A .unl link endpoint has conflicting interface names.")
            interface_name = endpoint_name or endpoint_interface
            display_label = _xml_text(raw_endpoint.get("label"), "link endpoint label", 128)
            if endpoint_kind == "node":
                declared_interfaces = node_by_id[endpoint_id]["interfaces"]
                if interface_id and not any(item["id"] == interface_id for item in declared_interfaces):
                    raise PnetlabArchiveError("A .unl link references an undeclared node interface.")
                declared = next(
                    (item for item in declared_interfaces if item["id"] == interface_id), None
                ) if interface_id else None
                if not interface_id and interface_name and not any(
                    item["name"] == interface_name for item in declared_interfaces
                ):
                    raise PnetlabArchiveError("A .unl link references an undeclared node interface.")
                if (
                    interface_name
                    and declared
                    and declared["name"]
                    and interface_name != declared["name"]
                ):
                    raise PnetlabArchiveError("A .unl link interface name conflicts with node metadata.")
                if not interface_id and not interface_name and display_label:
                    warnings.append(
                        f"Link {link_id or '?'} node endpoint {endpoint_id} has only a display label, not an interface reference."
                    )
                elif not interface_id and not interface_name:
                    warnings.append(f"Link {link_id or '?'} node endpoint {endpoint_id} has no interface reference.")
            endpoint = {
                "kind": endpoint_kind,
                "id": endpoint_id,
                "interface_id": interface_id,
                "interface": interface_name,
            }
            if display_label is not None:
                endpoint["label"] = display_label
            endpoints.append(endpoint)
        kind = "direct" if all(endpoint["kind"] == "node" for endpoint in endpoints) else "network"
        links.append({"id": link_id, "type": _xml_text(raw_link.get("type"), "link type", 64),
                      "kind": kind, "endpoints": endpoints})

    if not links and networks:
        if interface_networks:
            warnings.append("No explicit <links> records exist; interface network_id mappings are listed separately.")
        else:
            warnings.append("The file declares network objects but contains no links to them.")
    if any(child.tag == "objects" for child in list(root)):
        warnings.append("Non-topology lab objects (for example visual annotations) are not included in this analysis.")
    warnings.append(
        "Archive analysis is not connected to IP planning; unsupported PNETLab metadata is omitted."
    )
    return {
        "status": "ANALYZED_UNVERIFIED_SCHEMA",
        "preview_compatible": False,
        "source_member": member_name,
        "source_unl_sha256": hashlib.sha256(data).hexdigest(),
        "lab_name": _xml_text(root.get("name"), "lab name", 128),
        "nodes": nodes,
        "networks": networks,
        "interface_networks": interface_networks,
        "links": links,
        "warnings": list(dict.fromkeys(warnings)),
    }


def analyze_pnetlab_zip(filename: str | None, archive_bytes: bytes) -> dict:
    """Validate a ZIP and analyze its sole .unl member without extracting it."""
    _validate_upload_filename(filename)
    if not isinstance(archive_bytes, bytes) or not archive_bytes:
        raise PnetlabArchiveError("The uploaded ZIP is empty.")
    if len(archive_bytes) > MAX_UPLOAD_BYTES:
        raise PnetlabArchiveError("The uploaded ZIP exceeds the size limit.")

    try:
        _preflight_central_directory(archive_bytes)
        source = io.BytesIO(archive_bytes)
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
                raise PnetlabArchiveError("The ZIP has no files or contains too many entries.")
            names = set()
            unl_infos = []
            total_uncompressed = 0
            for info in infos:
                safe_name = _safe_member_name(info.filename)
                normalized = safe_name.casefold()
                if normalized in names:
                    raise PnetlabArchiveError("The ZIP contains duplicate filenames.")
                names.add(normalized)
                if info.flag_bits & 0x1:
                    raise PnetlabArchiveError("Encrypted ZIP entries are not supported.")
                if info.compress_type not in _ALLOWED_COMPRESSION:
                    raise PnetlabArchiveError("The ZIP uses an unsupported compression method.")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise PnetlabArchiveError("Symbolic links are not allowed in uploaded ZIPs.")
                file_type = stat.S_IFMT(mode)
                if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
                    raise PnetlabArchiveError("The ZIP contains a non-regular file entry.")
                if info.file_size < 0 or info.compress_size < 0 or info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
                    raise PnetlabArchiveError("A ZIP member exceeds the uncompressed size limit.")
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise PnetlabArchiveError("The ZIP exceeds the total uncompressed size limit.")
                if info.file_size and (
                    info.compress_size == 0
                    or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise PnetlabArchiveError("The ZIP contains an entry with an unsafe compression ratio.")
                if not info.is_dir() and safe_name.casefold().endswith(".unl"):
                    unl_infos.append((info, safe_name))
            if len(unl_infos) != 1:
                if not unl_infos:
                    raise PnetlabArchiveError("The ZIP does not contain a .unl topology file.")
                raise PnetlabArchiveError("The ZIP contains more than one .unl topology file.")
            unl_info, unl_name = unl_infos[0]
            if unl_info.file_size > MAX_UNL_BYTES:
                raise PnetlabArchiveError("The .unl topology file exceeds the size limit.")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise PnetlabArchiveError("The ZIP contains a member with a failed CRC check.")
            with archive.open(unl_info, "r") as handle:
                unl_bytes = handle.read(MAX_UNL_BYTES + 1)
            if len(unl_bytes) != unl_info.file_size:
                raise PnetlabArchiveError("The .unl topology file size did not match its ZIP metadata.")
    except PnetlabArchiveError:
        raise
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, ValueError, zlib.error) as exc:
        raise PnetlabArchiveError("The uploaded file is not a valid, readable ZIP archive.") from exc

    result = _parse_unl(unl_bytes, unl_name)
    result["source_archive_sha256"] = hashlib.sha256(archive_bytes).hexdigest()
    result["source_archive_filename"] = filename
    return result
