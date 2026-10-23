import csv
import hashlib
import io
import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from network_copilot.lab_bootstrap import safe_export
from network_copilot.lab_bootstrap.planner import BootstrapPlanError, build_plan
from network_copilot.lab_bootstrap.topology_adapter import normalize_topology_data


def _topology_data(extra_node_fields=None, direct_link=True):
    extra_node_fields = extra_node_fields or {}
    nodes = {
        node_id: {
            "name": f"Router {node_id}",
            "template": "Cisco vIOS Router",
            **extra_node_fields,
        }
        for node_id in ("1", "2")
    }
    topology = [
        {"source": "node1", "source_type": "node", "source_label": "Gi0/0",
         "destination": "net1", "destination_type": "network"},
        {"source": "node2", "source_type": "node", "source_label": "Gi0/0",
         "destination": "net1", "destination_type": "network"},
    ]
    if direct_link:
        topology.append(
            {"source": "node1", "source_type": "node", "source_label": "Gi0/1",
             "destination": "node2", "destination_type": "node", "destination_label": "Gi0/1"}
        )
    return {"nodes": nodes, "topology": topology}


def _plan(management_segment_id="network:net1"):
    data = _topology_data()
    nodes, topology = normalize_topology_data(data)
    return build_plan(
        nodes,
        topology,
        management_segment_id=management_segment_id,
        management_pool="172.16.3.0/24",
        data_pool="10.100.0.0/16",
    )


def _rewrite_zip(entries):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return output.getvalue()


def test_safe_export_zip_has_preview_manifest_csv_readme_and_checksums_only():
    plan = _plan()
    package = safe_export.build_safe_export_package(plan, project_id=17)

    with ZipFile(io.BytesIO(package)) as archive:
        assert set(archive.namelist()) == {
            "README.txt", "checksums.txt", "ip-plan.csv", "manifest.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["package_type"] == "pnetlab-ip-plan-preview"
        assert manifest["status"] == "PLANNED"
        assert manifest["gateway_status"] == "NOT_SUPPORTED"
        assert manifest["project_id"] == 17
        assert manifest["plan_id"] == plan["plan_id"]
        assert manifest["management_segment_id"] == "network:net1"
        assert manifest["topology_included"] is False
        assert manifest["topology_file"] is None
        assert manifest["configuration_artifacts"] == []
        assert manifest["applied"] is False
        assert manifest["verified"] is False
        assert all(node["image_build"] is None for node in manifest["nodes"])
        assert all(node["configuration_status"] == "NEEDS_MANUAL_ACTION" for node in manifest["nodes"])
        assert all(node["ssh_status"] == "NEEDS_MANUAL_ACTION" for node in manifest["nodes"])
        assert next(segment for segment in manifest["segments"] if segment["id"] == "cable:2")["kind"] == "direct"
        assert any("No source .unl is attached" in warning for warning in manifest["warnings"])

        csv_rows = list(csv.DictReader(io.StringIO(archive.read("ip-plan.csv").decode("utf-8"))))
        assert {row["node_id"] for row in csv_rows} == {"1", "2"}
        assert {row["segment_kind"] for row in csv_rows} == {"network", "direct"}
        assert all(row["ip"] and row["interface"] and row["subnet"] for row in csv_rows)
        readme = archive.read("README.txt").decode("utf-8")
        assert "Status: PLANNED (not applied, imported, configured, or verified)" in readme
        assert "This is a review/export package, not a PNETLab lab-import archive." in readme
        assert "A separately analyzed source archive is not attached" in readme
        assert "Gateway status: NOT_SUPPORTED" in readme
        checksums = archive.read("checksums.txt").decode("ascii").splitlines()
        for line in checksums:
            digest, filename = line.split("  ", 1)
            assert hashlib.sha256(archive.read(filename)).hexdigest() == digest


def test_safe_export_round_trips_through_preview_analyzer_as_unverified_inspection_only():
    plan = _plan()
    preserved_warning = "Fixture warning: source topology equivalence has not been established."
    plan["warnings"].append(preserved_warning)
    package = safe_export.build_safe_export_package(plan, project_id=17)

    result = safe_export.analyze_safe_export_package(package)

    assert result["status"] == "ANALYZED_UNVERIFIED_EXPORT"
    assert result["checksums_verified"] is True
    assert result["preview_compatible"] is False
    assert result["import_compatible"] is False
    assert result["manifest"]["status"] == "PLANNED"
    assert result["manifest"]["applied"] is False
    assert result["manifest"]["verified"] is False
    assert result["manifest"]["gateway_status"] == "NOT_SUPPORTED"
    assert result["manifest"]["configuration_artifacts"] == []
    assert preserved_warning in result["manifest"]["warnings"]
    assert result["inspection"]["planner_eligible"] is False
    assert preserved_warning in result["inspection"]["issues"]
    assert {segment["kind"] for segment in result["inspection"]["segments"]} == {"direct", "network"}
    assert result["files"] == ["README.txt", "checksums.txt", "ip-plan.csv", "manifest.json"]


def test_safe_export_analyzer_rejects_corrupt_missing_and_traversal_archives():
    package = safe_export.build_safe_export_package(_plan(), project_id=17)
    with pytest.raises(safe_export.SafeExportError, match="malformed"):
        safe_export.analyze_safe_export_package(b"not a zip")

    with ZipFile(io.BytesIO(package)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    missing = dict(entries)
    missing.pop("README.txt")
    with pytest.raises(safe_export.SafeExportError, match="exactly the four"):
        safe_export.analyze_safe_export_package(_rewrite_zip(missing))

    traversal = dict(entries)
    traversal["../manifest.json"] = traversal.pop("manifest.json")
    with pytest.raises(safe_export.SafeExportError, match="unsafe member"):
        safe_export.analyze_safe_export_package(_rewrite_zip(traversal))

    corrupt = dict(entries)
    corrupt["ip-plan.csv"] += b"altered\n"
    with pytest.raises(safe_export.SafeExportError, match="checksum"):
        safe_export.analyze_safe_export_package(_rewrite_zip(corrupt))

    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate_stream = io.BytesIO()
        with ZipFile(duplicate_stream, "w") as duplicate_archive:
            for name, value in entries.items():
                duplicate_archive.writestr(name, value)
            duplicate_archive.writestr("manifest.json", entries["manifest.json"])
    with pytest.raises(safe_export.SafeExportError, match="duplicate filenames"):
        safe_export.analyze_safe_export_package(duplicate_stream.getvalue())


def test_safe_export_analyzer_rejects_input_over_configured_size_limit(monkeypatch):
    package = safe_export.build_safe_export_package(_plan(), project_id=17)
    monkeypatch.setattr(safe_export, "MAX_EXPORT_BYTES", len(package) - 1)
    with pytest.raises(safe_export.SafeExportError, match="exceeds the size limit"):
        safe_export.analyze_safe_export_package(package)


def test_safe_export_analyzer_rejects_manifest_that_claims_applied_state_even_if_rechecksummed():
    package = safe_export.build_safe_export_package(_plan(), project_id=17)
    with ZipFile(io.BytesIO(package)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["applied"] = True
    entries["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    entries["checksums.txt"] = "".join(
        f"{hashlib.sha256(entries[name]).hexdigest()}  {name}\n"
        for name in sorted(set(entries) - {"checksums.txt"})
    ).encode("ascii")
    with pytest.raises(safe_export.SafeExportError, match="unsafe state"):
        safe_export.analyze_safe_export_package(_rewrite_zip(entries))


def test_safe_export_is_reproducible_and_does_not_leak_credentials_or_discovery_token():
    plan = _plan()
    plan["irrelevant_discovery_token"] = "fixture-only-token-value"
    first = safe_export.build_safe_export_package(plan, project_id=17)
    second = safe_export.build_safe_export_package(plan, project_id=17)
    assert first == second
    assert b"fixture-only-token-value" not in first
    assert b"startup-config/" not in first

    unsafe = _plan()
    unsafe["nodes"][0]["name"] = "Router password fixture-only-secret"
    with pytest.raises(safe_export.SafeExportError, match="Credential-like text"):
        safe_export.build_safe_export_package(unsafe, project_id=17)


def test_safe_export_rejects_incomplete_management_and_path_traversal():
    no_management = _plan(management_segment_id=None)
    with pytest.raises(safe_export.SafeExportError, match="complete PLANNED"):
        safe_export.build_safe_export_package(no_management, project_id=17)

    plan = _plan()
    plan["nodes"][0]["node_id"] = "../outside"
    with pytest.raises(safe_export.SafeExportError, match="invalid or duplicate node ID"):
        safe_export.build_safe_export_package(plan, project_id=17)

    with pytest.raises(BootstrapPlanError, match="not a discovered PNETLab network"):
        _plan(management_segment_id="cable:2")


def test_safe_export_rejects_duplicate_addresses_and_inconsistent_plan():
    plan = _plan()
    plan["nodes"][1]["addresses"][0]["ip"] = plan["nodes"][0]["addresses"][0]["ip"]
    with pytest.raises(safe_export.SafeExportError, match="inconsistent or duplicates an IP"):
        safe_export.build_safe_export_package(plan, project_id=17)

    plan = _plan()
    plan["manifest"]["plan_id"] = "f" * 64
    with pytest.raises(safe_export.SafeExportError, match="does not match the plan"):
        safe_export.build_safe_export_package(plan, project_id=17)


def _unverified_inspection_result(*, issues=None):
    return (
        {
            "status": "ANALYZED_UNVERIFIED_SCHEMA",
            "preview_compatible": False,
            "source_archive_sha256": "a" * 64,
            "source_unl_sha256": "b" * 64,
            "nodes": [{"id": "1", "name": "Sensitive Router Name", "interfaces": [{"id": "0"}]}],
            "networks": [{"id": "net-1", "name": "private management", "type": "bridge"}],
            "links": [{"kind": "network"}],
            "interface_networks": [{"node_id": "1", "network_id": "net-1"}],
            "warnings": ["Schema remains unverified."],
        },
        {
            "status": "INSPECTION_ONLY",
            "planner_eligible": False,
            "segments": [{"id": "network:net-1", "kind": "network"}],
            "blockers": ["Schema remains unverified."],
            "issues": list(issues or []),
        },
    )


def test_unverified_inspection_report_contains_summary_only_with_reproducible_checksums():
    analysis, inspection = _unverified_inspection_result()

    package = safe_export.build_safe_inspection_report_package(analysis, inspection)
    with ZipFile(BytesIO(package)) as archive:
        assert set(archive.namelist()) == {"README.txt", "checksums.txt", "manifest.json"}
        readme = archive.read("README.txt")
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest == {
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
            "source_archive_sha256": "a" * 64,
            "source_unl_sha256": "b" * 64,
            "counts": {
                "nodes": 1,
                "interfaces": 1,
                "networks": 1,
                "direct_links": 0,
                "network_links": 1,
                "interface_network_attachments": 1,
                "inspected_segments": 1,
                "warnings": 1,
                "blockers": 1,
                "issues": 0,
            },
        }
        assert b"Sensitive Router Name" not in package
        assert b"private management" not in package
        assert b".unl" not in package
        assert b"startup-config" not in package
        assert b"NOT_RUN" in readme
        assert b"not a PNETLab import archive" in readme
        for line in archive.read("checksums.txt").decode("ascii").splitlines():
            digest, filename = line.split("  ", 1)
            assert hashlib.sha256(archive.read(filename)).hexdigest() == digest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda analysis, inspection: analysis.update(preview_compatible=True),
        lambda analysis, inspection: inspection.update(planner_eligible=True),
        lambda analysis, inspection: inspection.update(issues=["invalid interface"]),
        lambda analysis, inspection: analysis.update(status="ANALYZED_VERIFIED_SCHEMA"),
    ],
)
def test_unverified_inspection_report_rejects_ineligible_or_invalid_state(mutate):
    analysis, inspection = _unverified_inspection_result()
    mutate(analysis, inspection)

    with pytest.raises(safe_export.SafeExportError):
        safe_export.build_safe_inspection_report_package(analysis, inspection)
