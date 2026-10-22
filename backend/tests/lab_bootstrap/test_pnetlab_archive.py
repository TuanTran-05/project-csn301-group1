from io import BytesIO
import hashlib
import json
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from network_copilot.lab_bootstrap import pnetlab_archive


UNL_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<lab name="Archive fixture">
  <topology>
    <nodes>
      <node id="1" name="vIOS1" type="qemu" template="Cisco vIOS Router"
            username="ignored-user-field" password="ignored-password-field"
            image="vios-adventerprisek9-m.SPA.159-3.M6" image_build="15.9(3)M6" qemu_version="2.12.0">
        <interface id="0" name="Gi0/0" />
        <interface id="1" name="Gi0/1" />
      </node>
      <node id="2" name="vIOS2" type="qemu" template="Cisco vIOS Router"
            image="vios-adventerprisek9-m.SPA.159-3.M6" qemu_version="2.12.0">
        <interface id="0" name="Gi0/0" />
      </node>
    </nodes>
    <networks>
      <network id="net1" name="Management bridge" type="bridge" />
    </networks>
    <links>
      <link id="1" type="ethernet">
        <interface1 node="1" id="0" name="Gi0/0" />
        <interface2 network="net1" />
      </link>
      <link id="2" type="ethernet">
        <interface1 node="1" id="1" name="Gi0/1" />
        <interface2 node="2" id="0" name="Gi0/0" />
      </link>
    </links>
  </topology>
</lab>
'''

PNETLAB_NETWORK_ID_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<lab name="Sanitized PNETLab bridge fixture">
  <topology>
    <nodes>
      <node id="1" name="vIOS1" type="qemu" template="vios"
            image="vios-adventerprisek9-m.SPA.159-3.M6" qemu_version="2.12.0">
        <interface id="0" name="Gi0/0" type="ethernet" network_id="1" label="" />
      </node>
      <node id="2" name="vIOS2" type="qemu" template="vios"
            image="vios-adventerprisek9-m.SPA.159-3.M6" qemu_version="2.12.0">
        <interface id="0" name="Gi0/0" type="ethernet" network_id="1" label="" />
      </node>
    </nodes>
    <networks>
      <network id="1" name="vIOS1iface_0" type="bridge" left="318" top="222" />
    </networks>
  </topology>
  <objects><textobjects><textobject id="1" name="label" type="text"><data /></textobject></textobjects></objects>
</lab>
'''


SYNTHETIC_BRIDGE_ATTACHMENT_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<lab name="Synthetic bridge attachment fixture">
  <topology>
    <nodes>
      <node id="10" name="Router A" type="qemu" template="generic-router"
            image="synthetic-router.img" qemu_version="2.12.0">
        <interface id="0" name="eth0" type="ethernet" network_id="90" label="test-port" />
      </node>
      <node id="20" name="Router B" type="qemu" template="generic-router"
            image="synthetic-router.img" qemu_version="2.12.0">
        <interface id="0" name="eth0" type="ethernet" network_id="90" label="test-port" />
      </node>
    </nodes>
    <networks>
      <network id="90" name="Shared test bridge" type="bridge" canvas_x="10" />
    </networks>
  </topology>
  <objects><annotations><note id="1">Synthetic annotation</note></annotations></objects>
</lab>
'''


def _zip_bytes(entries=None):
    entries = entries if entries is not None else [("test.unl", UNL_XML)]
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return output.getvalue()


def _assert_rejected_before_zipfile(monkeypatch, archive_bytes, message):
    def fail_if_zipfile_is_constructed(*args, **kwargs):
        raise AssertionError("Preflight must reject this archive before ZipFile")

    monkeypatch.setattr(pnetlab_archive.zipfile, "ZipFile", fail_if_zipfile_is_constructed)
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match=message):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", archive_bytes)


def _multipart(archive_bytes, filename="test-export.zip"):
    return {"file": (BytesIO(archive_bytes), filename)}


def test_analyze_zip_preserves_nodes_images_interfaces_and_link_kinds():
    archive_bytes = _zip_bytes([("topology/test.unl", UNL_XML)])
    result = pnetlab_archive.analyze_pnetlab_zip("test-export.zip", archive_bytes)

    assert result["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert result["preview_compatible"] is False
    assert result["source_member"] == "topology/test.unl"
    assert result["nodes"] == [
        {
            "id": "1", "name": "vIOS1", "type": "qemu",
            "template": "Cisco vIOS Router",
            "image": "vios-adventerprisek9-m.SPA.159-3.M6",
            "image_build": "15.9(3)M6", "qemu_version": "2.12.0",
            "interfaces": [
                {"id": "0", "name": "Gi0/0"},
                {"id": "1", "name": "Gi0/1"},
            ],
        },
        {
            "id": "2", "name": "vIOS2", "type": "qemu",
            "template": "Cisco vIOS Router",
            "image": "vios-adventerprisek9-m.SPA.159-3.M6",
            "image_build": None, "qemu_version": "2.12.0",
            "interfaces": [{"id": "0", "name": "Gi0/0"}],
        },
    ]
    assert result["networks"] == [{"id": "net1", "name": "Management bridge", "type": "bridge"}]
    assert result["links"][0] == {
        "id": "1", "type": "ethernet", "kind": "network",
        "endpoints": [
            {"kind": "node", "id": "1", "interface_id": "0", "interface": "Gi0/0"},
            {"kind": "network", "id": "net1", "interface_id": None, "interface": None},
        ],
    }
    assert result["links"][1]["kind"] == "direct"
    assert [endpoint["id"] for endpoint in result["links"][1]["endpoints"]] == ["1", "2"]
    assert result["links"][1]["endpoints"][0]["interface"] == "Gi0/1"
    assert result["links"][1]["endpoints"][1]["interface"] == "Gi0/0"
    assert any("not connected to IP planning" in warning for warning in result["warnings"])
    assert result["source_unl_sha256"] == hashlib.sha256(UNL_XML).hexdigest()
    assert result["source_archive_sha256"] == hashlib.sha256(archive_bytes).hexdigest()
    assert "ignored-user-field" not in json.dumps(result)
    assert "ignored-password-field" not in json.dumps(result)


def test_analyze_pnetlab_network_id_layout_preserves_bridge_attachments_without_inventing_links():
    archive_bytes = _zip_bytes([("test.unl", PNETLAB_NETWORK_ID_XML)])
    result = pnetlab_archive.analyze_pnetlab_zip(
        "_Exports_pnetlab_export-20261021-192746.zip", archive_bytes
    )

    assert result["source_member"] == "test.unl"
    assert result["nodes"] == [
        {
            "id": "1", "name": "vIOS1", "type": "qemu", "template": "vios",
            "image": "vios-adventerprisek9-m.SPA.159-3.M6", "image_build": None,
            "qemu_version": "2.12.0",
            "interfaces": [{
                "id": "0", "name": "Gi0/0", "type": "ethernet",
                "network_id": "1", "label": "",
            }],
        },
        {
            "id": "2", "name": "vIOS2", "type": "qemu", "template": "vios",
            "image": "vios-adventerprisek9-m.SPA.159-3.M6", "image_build": None,
            "qemu_version": "2.12.0",
            "interfaces": [{
                "id": "0", "name": "Gi0/0", "type": "ethernet",
                "network_id": "1", "label": "",
            }],
        },
    ]
    assert result["networks"] == [{"id": "1", "name": "vIOS1iface_0", "type": "bridge"}]
    assert result["interface_networks"] == [
        {
            "node_id": "1", "interface_id": "0", "interface": "Gi0/0",
            "network_id": "1", "network_type": "bridge",
        },
        {
            "node_id": "2", "interface_id": "0", "interface": "Gi0/0",
            "network_id": "1", "network_type": "bridge",
        },
    ]
    assert result["links"] == [], "network_id attachments must not be reinterpreted as direct cables"
    assert result["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert result["preview_compatible"] is False
    assert result["source_unl_sha256"] == hashlib.sha256(PNETLAB_NETWORK_ID_XML).hexdigest()
    assert any("interface network_id mappings are listed separately" in item for item in result["warnings"])
    assert any("additional metadata omitted" in item for item in result["warnings"])


def test_synthetic_shared_bridge_preserves_attachments_without_inventing_links():
    result = pnetlab_archive.analyze_pnetlab_zip(
        "synthetic-bridge-export.zip",
        _zip_bytes([("topology/test.unl", SYNTHETIC_BRIDGE_ATTACHMENT_XML)]),
    )

    assert result["nodes"] == [
        {
            "id": "10", "name": "Router A", "type": "qemu", "template": "generic-router",
            "image": "synthetic-router.img", "image_build": None,
            "qemu_version": "2.12.0",
            "interfaces": [{
                "id": "0", "name": "eth0", "type": "ethernet",
                "network_id": "90", "label": "test-port",
            }],
        },
        {
            "id": "20", "name": "Router B", "type": "qemu", "template": "generic-router",
            "image": "synthetic-router.img", "image_build": None,
            "qemu_version": "2.12.0",
            "interfaces": [{
                "id": "0", "name": "eth0", "type": "ethernet",
                "network_id": "90", "label": "test-port",
            }],
        },
    ]
    assert result["networks"] == [{"id": "90", "name": "Shared test bridge", "type": "bridge"}]
    assert result["interface_networks"] == [
        {"node_id": "10", "interface_id": "0", "interface": "eth0", "network_id": "90", "network_type": "bridge"},
        {"node_id": "20", "interface_id": "0", "interface": "eth0", "network_id": "90", "network_type": "bridge"},
    ]
    assert result["links"] == []
    assert result["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert result["preview_compatible"] is False
    assert all("image_build" not in node or node["image_build"] is None for node in result["nodes"])
    assert result["source_unl_sha256"] == hashlib.sha256(SYNTHETIC_BRIDGE_ATTACHMENT_XML).hexdigest()
    assert any("interface network_id mappings are listed separately" in warning for warning in result["warnings"])
    assert any("additional metadata omitted" in warning for warning in result["warnings"])
    assert any("Non-topology lab objects" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("xml", "warning_prefix"),
    [
        (
            UNL_XML.replace(
                b"</lab>", b"<extension_fixture>safe-marker</extension_fixture></lab>"
            ),
            "Lab structure",
        ),
        (
            UNL_XML.replace(
                b"</topology>",
                b"<extension_fixture>safe-marker</extension_fixture></topology>",
            ),
            "Topology structure",
        ),
        (
            UNL_XML.replace(
                b"</nodes>", b"<extension_fixture>safe-marker</extension_fixture></nodes>"
            ),
            "Node collection",
        ),
        (
            UNL_XML.replace(
                b"</node>", b"<extension_fixture>safe-marker</extension_fixture></node>", 1
            ),
            "Node metadata",
        ),
        (
            UNL_XML.replace(
                b'<interface id="0" name="Gi0/0" />',
                b'<interface id="0" name="Gi0/0"><extension_fixture>safe-marker</extension_fixture></interface>',
                1,
            ),
            "Interface metadata",
        ),
        (
            UNL_XML.replace(
                b"</networks>",
                b"<extension_fixture>safe-marker</extension_fixture></networks>",
            ),
            "Network collection",
        ),
        (
            UNL_XML.replace(
                b'<network id="net1" name="Management bridge" type="bridge" />',
                b'<network id="net1" name="Management bridge" type="bridge"><extension_fixture>safe-marker</extension_fixture></network>',
            ),
            "Network metadata",
        ),
        (
            UNL_XML.replace(
                b"</links>", b"<extension_fixture>safe-marker</extension_fixture></links>"
            ),
            "Link collection",
        ),
        (
            UNL_XML.replace(
                b"</link>", b"<extension_fixture>safe-marker</extension_fixture></link>", 1
            ),
            "Link metadata",
        ),
        (
            UNL_XML.replace(
                b'<interface2 network="net1" />',
                b'<interface2 network="net1"><extension_fixture>safe-marker</extension_fixture></interface2>',
                1,
            ),
            "Link endpoint metadata",
        ),
    ],
)
def test_unmodeled_xml_child_elements_are_warned_and_not_returned(xml, warning_prefix):
    result = pnetlab_archive.analyze_pnetlab_zip(
        "test.zip", _zip_bytes([("topology/test.unl", xml)])
    )

    assert any(
        warning.startswith(warning_prefix) and "unsupported child elements" in warning
        for warning in result["warnings"]
    )
    assert "safe-marker" not in json.dumps(result)
    assert result["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert result["preview_compatible"] is False


@pytest.mark.parametrize("xml", [b"<lab><topology>", b"<!DOCTYPE lab [<!ENTITY x 'boom'>]><lab />"])
def test_analyze_rejects_malformed_xml_and_entity_declarations(xml):
    with pytest.raises(pnetlab_archive.PnetlabArchiveError):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("test.unl", xml)]))


def test_valid_xml_outside_supported_unl_layout_is_classified_as_unsupported_schema():
    with pytest.raises(pnetlab_archive.UnsupportedPnetlabSchemaError, match="root must be <lab>"):
        pnetlab_archive.analyze_pnetlab_zip(
            "test.zip", _zip_bytes([("test.unl", b"<topology />")])
        )


def test_supported_unl_layout_without_nodes_is_classified_as_incomplete_topology():
    xml = b"<lab><topology><nodes /><networks /></topology></lab>"
    with pytest.raises(pnetlab_archive.IncompletePnetlabTopologyError, match="does not contain any nodes"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("test.unl", xml)]))


def test_analyze_rejects_invalid_zip_and_unsafe_or_duplicate_members():
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="valid, readable ZIP"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", b"not a zip")
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="unsafe member path"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("../test.unl", UNL_XML)]))
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="duplicate filenames"):
        pnetlab_archive.analyze_pnetlab_zip(
            "test.zip", _zip_bytes([("test.unl", UNL_XML), ("TEST.UNL", UNL_XML)])
        )

    compressible = _zip_bytes([("test.unl", b"A" * 20_000)])
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="compression ratio"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", compressible)

    stored = BytesIO()
    with ZipFile(stored, "w", compression=ZIP_STORED) as archive:
        archive.writestr("test.unl", UNL_XML)
    corrupted = bytearray(stored.getvalue())
    corrupted[corrupted.index(b"vIOS1")] ^= 1
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="CRC"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", bytes(corrupted))


def test_entry_limit_is_checked_before_stdlib_zipfile_materializes_entries(monkeypatch):
    archive_bytes = _zip_bytes([
        ("test.unl", UNL_XML),
        *((f"assets/{index}.txt", b"x") for index in range(pnetlab_archive.MAX_ARCHIVE_ENTRIES)),
    ])

    _assert_rejected_before_zipfile(monkeypatch, archive_bytes, "too many entries")


def test_actual_entry_count_limit_cannot_be_hidden_by_lower_eocd_count(monkeypatch):
    archive_bytes = bytearray(_zip_bytes([
        ("test.unl", UNL_XML),
        *((f"assets/{index}.txt", b"x") for index in range(pnetlab_archive.MAX_ARCHIVE_ENTRIES)),
    ]))
    eocd_offset = archive_bytes.rfind(pnetlab_archive._EOCD_SIGNATURE)
    # Advertise 256 records while the central directory actually contains 257.
    archive_bytes[eocd_offset + 8:eocd_offset + 12] = b"\x00\x01\x00\x01"

    _assert_rejected_before_zipfile(monkeypatch, bytes(archive_bytes), "too many entries")


def test_preflight_rejects_malformed_and_inconsistent_central_directory(monkeypatch):
    archive_bytes = _zip_bytes()
    malformed_directory = bytearray(archive_bytes)
    directory_offset = malformed_directory.find(pnetlab_archive._CENTRAL_DIRECTORY_SIGNATURE)
    malformed_directory[directory_offset:directory_offset + 4] = b"NOPE"
    _assert_rejected_before_zipfile(
        monkeypatch, bytes(malformed_directory), "malformed central-directory"
    )

    inconsistent_count = bytearray(archive_bytes)
    eocd_offset = inconsistent_count.rfind(pnetlab_archive._EOCD_SIGNATURE)
    inconsistent_count[eocd_offset + 8:eocd_offset + 12] = b"\x02\x00\x02\x00"
    _assert_rejected_before_zipfile(
        monkeypatch, bytes(inconsistent_count), "count is inconsistent"
    )


def test_preflight_rejects_zip64_metadata_before_zipfile_construction(monkeypatch):
    archive_bytes = bytearray(_zip_bytes())
    eocd_offset = archive_bytes.rfind(pnetlab_archive._EOCD_SIGNATURE)
    archive_bytes[eocd_offset + 10:eocd_offset + 12] = b"\xff\xff"

    _assert_rejected_before_zipfile(monkeypatch, bytes(archive_bytes), "ZIP64 archives")


def test_preflight_rejects_zip64_locator_before_zipfile_construction(monkeypatch):
    archive_bytes = bytearray(_zip_bytes())
    eocd_offset = archive_bytes.rfind(pnetlab_archive._EOCD_SIGNATURE)
    zip64_locator = pnetlab_archive._ZIP64_LOCATOR_SIGNATURE + b"\x00" * 16
    archive_bytes[eocd_offset:eocd_offset] = zip64_locator

    _assert_rejected_before_zipfile(monkeypatch, bytes(archive_bytes), "ZIP64 archives")


def test_preflight_rejects_truncated_zip_before_zipfile_construction(monkeypatch):
    archive_bytes = _zip_bytes()[:-4]

    _assert_rejected_before_zipfile(
        monkeypatch, archive_bytes, "valid, readable ZIP"
    )


def test_preflight_safely_rejects_false_eocd_signature_in_archive_comment(monkeypatch):
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        archive.writestr("test.unl", UNL_XML)
        archive.comment = b"comment-with-signature-" + pnetlab_archive._EOCD_SIGNATURE

    # The archive is valid ZIP data, but rfind selects the signature in its comment.
    # The parser must reject it before the standard library sees ambiguous metadata.
    _assert_rejected_before_zipfile(
        monkeypatch, output.getvalue(), "valid, readable ZIP"
    )


def test_preflight_rejects_out_of_range_central_directory_offset(monkeypatch):
    archive_bytes = bytearray(_zip_bytes())
    eocd_offset = archive_bytes.rfind(pnetlab_archive._EOCD_SIGNATURE)
    archive_bytes[eocd_offset + 16:eocd_offset + 20] = (len(archive_bytes) + 1).to_bytes(4, "little")

    _assert_rejected_before_zipfile(
        monkeypatch, bytes(archive_bytes), "central-directory boundaries"
    )


def test_preflight_rejects_multidisk_metadata_before_zipfile_construction(monkeypatch):
    archive_bytes = bytearray(_zip_bytes())
    eocd_offset = archive_bytes.rfind(pnetlab_archive._EOCD_SIGNATURE)
    archive_bytes[eocd_offset + 4:eocd_offset + 6] = b"\x01\x00"

    _assert_rejected_before_zipfile(monkeypatch, bytes(archive_bytes), "Multi-disk ZIP")


def test_preflight_rejects_trailing_data_after_archive_before_zipfile(monkeypatch):
    archive_bytes = _zip_bytes() + b"trailing-data"

    _assert_rejected_before_zipfile(
        monkeypatch, archive_bytes, "valid, readable ZIP"
    )


def test_analyze_rejects_missing_or_multiple_unl_files():
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="does not contain a .unl"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("readme.txt", b"lab")]))
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="more than one .unl"):
        pnetlab_archive.analyze_pnetlab_zip(
            "test.zip", _zip_bytes([("one.unl", UNL_XML), ("two.unl", UNL_XML)])
        )


def test_analyze_rejects_unusual_upload_filename_and_size_limit(monkeypatch):
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="simple filename"):
        pnetlab_archive.analyze_pnetlab_zip("../test.zip", _zip_bytes())
    monkeypatch.setattr(pnetlab_archive, "MAX_UPLOAD_BYTES", 16)
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="size limit"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", b"x" * 17)


def test_analyze_rejects_unresolved_link_reference_and_does_not_guess_build():
    xml = UNL_XML.replace(b'network="net1"', b'network="missing"')
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="unknown network"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("test.unl", xml)]))

    xml = UNL_XML.replace(
        b'<interface1 node="1" id="1" name="Gi0/1"',
        b'<interface1 node="1" id="99" name="Gi0/1"',
    )
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="undeclared node interface"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("test.unl", xml)]))


def test_link_endpoint_with_empty_node_and_network_attributes_is_rejected_as_ambiguous():
    xml = UNL_XML.replace(
        b'<interface1 node="1" id="0" name="Gi0/0" />',
        b'<interface1 node="" network="net1" id="0" name="Gi0/0" />',
        1,
    )

    with pytest.raises(
        pnetlab_archive.UnsupportedPnetlabSchemaError,
        match="must reference one node or network",
    ):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("test.unl", xml)]))


def test_link_endpoint_name_without_id_must_match_a_declared_interface():
    xml = UNL_XML.replace(
        b'<interface1 node="1" id="0" name="Gi0/0" />',
        b'<interface1 node="1" name="Gi9/9" />',
        1,
    )
    with pytest.raises(pnetlab_archive.PnetlabArchiveError, match="undeclared node interface"):
        pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("test.unl", xml)]))


def test_link_display_label_is_preserved_but_never_used_as_interface_identity():
    xml = UNL_XML.replace(
        b'<interface1 node="1" id="0" name="Gi0/0" />',
        b'<interface1 node="1" label="Gi0/0" />',
        1,
    )
    result = pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("test.unl", xml)]))
    endpoint = result["links"][0]["endpoints"][0]

    assert endpoint["interface_id"] is None
    assert endpoint["interface"] is None
    assert endpoint["label"] == "Gi0/0"
    assert any("only a display label, not an interface reference" in item for item in result["warnings"])

    result = pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes())
    assert result["nodes"][1]["image_build"] is None
    assert result["nodes"][1]["qemu_version"] == "2.12.0"


def test_missing_interface_label_stays_unknown_instead_of_being_guessed():
    xml = UNL_XML.replace(b'<interface id="0" name="Gi0/0" />', b'<interface id="0" />', 1)
    result = pnetlab_archive.analyze_pnetlab_zip("test.zip", _zip_bytes([("test.unl", xml)]))
    assert result["nodes"][0]["interfaces"][0] == {"id": "0", "name": None}
    assert result["links"][0]["endpoints"][0]["interface"] == "Gi0/0"
    assert any("interface 0 has no interface name" in warning for warning in result["warnings"])
