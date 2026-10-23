from network_copilot.lab_bootstrap import archive_inspection, pnetlab_archive

from test_pnetlab_archive import (
    PNETLAB_NETWORK_ID_XML,
    SYNTHETIC_BRIDGE_ATTACHMENT_XML,
    UNL_XML,
    _zip_bytes,
)


def _inspect(xml):
    analysis = pnetlab_archive.analyze_pnetlab_zip(
        "fixture-export.zip", _zip_bytes([("test.unl", xml)])
    )
    return analysis, archive_inspection.inspect_archive_analysis(analysis)


def test_unverified_network_id_fixture_is_inspected_as_network_not_direct_or_planned():
    analysis, inspection = _inspect(PNETLAB_NETWORK_ID_XML)

    assert analysis["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert analysis["preview_compatible"] is False
    assert analysis["links"] == []
    assert inspection["status"] == "INSPECTION_ONLY"
    assert inspection["planner_eligible"] is False
    assert inspection["relationship_count"] == 2
    assert [segment["id"] for segment in inspection["segments"]] == ["network:1"]
    assert inspection["segments"][0]["kind"] == "network"
    assert [endpoint["node_id"] for endpoint in inspection["segments"][0]["endpoints"]] == ["1", "2"]
    assert [endpoint["interface"] for endpoint in inspection["segments"][0]["endpoints"]] == [
        "GigabitEthernet0/0", "GigabitEthernet0/0",
    ]
    assert any("schema has not been verified" in item for item in inspection["blockers"])
    assert any("not mark this topology preview-compatible" in item for item in inspection["blockers"])


def test_synthetic_shared_bridge_inspects_as_network_and_keeps_planner_blocked():
    analysis, inspection = _inspect(SYNTHETIC_BRIDGE_ATTACHMENT_XML)

    assert analysis["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert analysis["preview_compatible"] is False
    assert analysis["links"] == []
    assert inspection["status"] == "INSPECTION_ONLY"
    assert inspection["planner_eligible"] is False
    assert inspection["relationship_count"] == 2
    assert [segment["id"] for segment in inspection["segments"]] == ["network:90"]
    assert inspection["segments"][0]["kind"] == "network"
    assert [endpoint["node_id"] for endpoint in inspection["segments"][0]["endpoints"]] == [
        "10", "20",
    ]
    assert all(segment["kind"] != "direct" for segment in inspection["segments"])
    assert any("schema has not been verified" in item for item in inspection["blockers"])
    assert any("metadata omitted by the parser" in item for item in inspection["blockers"])


def test_explicit_archive_cable_and_network_attachment_keep_distinct_segment_kinds():
    _analysis, inspection = _inspect(UNL_XML)

    assert inspection["planner_eligible"] is False
    assert {segment["kind"] for segment in inspection["segments"]} == {"direct", "network"}
    direct = next(segment for segment in inspection["segments"] if segment["kind"] == "direct")
    network = next(segment for segment in inspection["segments"] if segment["kind"] == "network")
    assert [endpoint["node_id"] for endpoint in direct["endpoints"]] == ["1", "2"]
    assert network["id"] == "network:net1"
    assert network["endpoints"][0]["interface"] == "GigabitEthernet0/0"


def test_network_object_without_any_attachment_is_not_connection_evidence():
    xml = PNETLAB_NETWORK_ID_XML.replace(b' network_id="1"', b"")
    _analysis, inspection = _inspect(xml)

    assert inspection["relationship_count"] == 0
    assert inspection["segments"] == []
    assert inspection["planner_eligible"] is False
    assert any("No explicit node links or interface-to-network attachments" in item
               for item in inspection["blockers"])


def test_missing_interface_name_is_reported_without_guessing_a_port():
    xml = PNETLAB_NETWORK_ID_XML.replace(b' name="Gi0/0"', b"", 1)
    _analysis, inspection = _inspect(xml)

    assert inspection["planner_eligible"] is False
    assert inspection["segments"] == []
    assert any("no interface was guessed" in item for item in inspection["issues"])


def test_archive_inspection_rejects_conflicting_interface_network_mappings():
    xml = UNL_XML.replace(
        b'<interface id="0" name="Gi0/0" />',
        b'<interface id="0" name="Gi0/0" network_id="net1" />',
        1,
    ).replace(
        b'<network id="net1" name="Management bridge" type="bridge" />',
        b'<network id="net1" name="Management bridge" type="bridge" />'
        b'<network id="net2" name="Second bridge" type="bridge" />',
    ).replace(
        b'<interface2 network="net1" />',
        b'<interface2 network="net2" />',
        1,
    )
    _analysis, inspection = _inspect(xml)

    assert inspection["planner_eligible"] is False
    assert any("attached to more than one archive network" in item for item in inspection["issues"])


def test_archive_inspection_rejects_mapping_to_undeclared_interface_even_if_flags_claim_verified():
    analysis = pnetlab_archive.analyze_pnetlab_zip(
        "fixture-export.zip", _zip_bytes([("test.unl", PNETLAB_NETWORK_ID_XML)])
    )
    analysis["status"] = "ANALYZED_VERIFIED_SCHEMA"
    analysis["preview_compatible"] = True
    analysis["warnings"] = []
    analysis["interface_networks"][0]["interface_id"] = "99"

    inspection = archive_inspection.inspect_archive_analysis(analysis)

    assert inspection["planner_eligible"] is False
    assert inspection["segments"] == []
    assert any("undeclared node interface" in issue for issue in inspection["issues"])


def test_both_verified_schema_and_preview_compatibility_are_required_for_planner_eligibility():
    analysis = pnetlab_archive.analyze_pnetlab_zip("fixture-export.zip", _zip_bytes())
    analysis["status"] = "ANALYZED_VERIFIED_SCHEMA"
    assert archive_inspection.inspect_archive_analysis(analysis)["planner_eligible"] is False

    analysis["status"] = "ANALYZED_UNVERIFIED_SCHEMA"
    analysis["preview_compatible"] = True
    assert archive_inspection.inspect_archive_analysis(analysis)["planner_eligible"] is False


def test_omitted_unsupported_metadata_blocks_planner_even_with_compatible_schema_flags():
    analysis = pnetlab_archive.analyze_pnetlab_zip(
        "fixture-export.zip", _zip_bytes([("test.unl", PNETLAB_NETWORK_ID_XML)])
    )
    analysis["status"] = "ANALYZED_VERIFIED_SCHEMA"
    analysis["preview_compatible"] = True
    analysis["warnings"] = [
        "Archive analysis is not connected to IP planning; unsupported PNETLab metadata is omitted."
    ]

    inspection = archive_inspection.inspect_archive_analysis(analysis)

    assert inspection["planner_eligible"] is False
    assert inspection["status"] == "INSPECTION_ONLY"
    assert any("metadata omitted by the parser" in item for item in inspection["blockers"])
