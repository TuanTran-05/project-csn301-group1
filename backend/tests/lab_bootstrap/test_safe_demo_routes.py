from __future__ import annotations

import builtins
import hashlib
import io
import json
import socket
from threading import BoundedSemaphore, Event, Thread
import zipfile
from datetime import timedelta

import pytest
from flask_jwt_extended import create_access_token

from network_copilot import lab_bootstrap_demo
from network_copilot.app import create_app
from network_copilot.config import TestConfig
from network_copilot.lab_bootstrap import planner


UNL_XML = b"""<lab name='Safe demo fixture'><topology><nodes>
<node id='1' name='vIOS1' template='vios' image='image-a'>
<interface id='0' name='Gi0/0' network_id='1'/></node>
<node id='2' name='vIOS2' template='vios' image='image-b'>
<interface id='0' name='Gi0/0' network_id='1'/></node>
</nodes><networks><network id='1' name='bridge' type='bridge'/></networks>
</topology></lab>"""


class RateLimitedDemoConfig(TestConfig):
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"


@pytest.fixture
def rate_limited_demo_client():
    app = create_app(RateLimitedDemoConfig)
    with app.app_context():
        token = create_access_token(identity="rate-limit-demo-user")
    yield app.test_client(), {"Authorization": f"Bearer {token}"}


def _zip_bytes(member_name: str = "topology/test.unl", payload: bytes = UNL_XML) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, payload)
    return output.getvalue()


def _upload(client, headers, payload: bytes, **request_options):
    return client.post(
        "/api/lab-bootstrap-demo/analyze-archive",
        data=payload,
        content_type="application/zip",
        headers=headers,
        **request_options,
    )


def _export_inspection(client, headers, payload: bytes, **request_options):
    return client.post(
        "/api/lab-bootstrap-demo/export-inspection",
        data=payload,
        content_type="application/zip",
        headers=headers,
        **request_options,
    )


def test_safe_demo_page_is_standalone(client):
    response = client.get("/lab-bootstrap-demo")

    assert response.status_code == 200
    assert b"Lab Bootstrap ZIP Safe Demo" in response.data
    assert b"lab_bootstrap_demo.js" in response.data
    assert b'id="export-inspection-button"' in response.data
    assert b"review-only" in response.data
    assert b'data-auth-mode="header"' in response.data
    assert b"JWT access token" in response.data
    assert not any(
        cookie.startswith("lab_bootstrap_demo_access=")
        for cookie in response.headers.getlist("Set-Cookie")
    )
    script = client.get("/static/js/lab_bootstrap_demo.js")
    assert script.status_code == 200
    assert b"/api/lab-bootstrap-demo/analyze-archive" in script.data
    assert b"/api/lab-bootstrap-demo/export-inspection" in script.data


def test_valid_upload_returns_server_analysis_but_never_runs_planner(
    client, admin_headers, monkeypatch
):
    def fail_if_planner_runs(*args, **kwargs):
        raise AssertionError("ZIP Safe Demo must not invoke the IP planner")

    monkeypatch.setattr(planner, "build_plan", fail_if_planner_runs)
    monkeypatch.setattr(socket, "socket", fail_if_planner_runs)
    monkeypatch.setattr(socket, "create_connection", fail_if_planner_runs)
    monkeypatch.setattr(builtins, "open", fail_if_planner_runs)

    response = _upload(client, admin_headers, _zip_bytes())

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert body["preview_compatible"] is False
    assert body["planner_eligible"] is False
    assert body["planner_status"] == "NOT_RUN"
    assert body["gateway_status"] == "NOT_SUPPORTED"
    assert body["device_actions"] == "NOT_PERFORMED"
    assert body["inspection"]["planner_eligible"] is False
    assert [segment["kind"] for segment in body["inspection"]["segments"]] == ["network"]
    assert body["analysis"]["nodes"][0]["name"] == "vIOS1"
    assert body["analysis"]["warnings"]
    assert any("no explicit image-build metadata" in warning for warning in body["analysis"]["warnings"])
    assert body["planner_blockers"]
    assert "ip_plan" not in body
    assert "startup_config" not in body
    assert "device_config" not in body


def test_inspection_export_is_summary_only_and_does_not_run_planner(
    client, admin_headers, monkeypatch
):
    def fail_if_planner_runs(*args, **kwargs):
        raise AssertionError("Safe Demo inspection export must not invoke the IP planner")

    monkeypatch.setattr(planner, "build_plan", fail_if_planner_runs)
    response = _export_inspection(client, admin_headers, _zip_bytes())

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "lab-bootstrap-inspection-report.zip" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(response.data)) as report:
        assert set(report.namelist()) == {"README.txt", "checksums.txt", "manifest.json"}
        manifest = json.loads(report.read("manifest.json"))
        assert manifest["archive_status"] == "ANALYZED_UNVERIFIED_SCHEMA"
        assert manifest["preview_compatible"] is False
        assert manifest["planner_status"] == "NOT_RUN"
        assert manifest["planner_eligible"] is False
        assert manifest["gateway_status"] == "NOT_SUPPORTED"
        assert manifest["applied"] is False
        assert manifest["verified"] is False
        assert manifest["configuration_artifacts"] == []
        assert manifest["source_archive_included"] is False
        assert b"vIOS1" not in response.data
        assert b"topology/test.unl" not in response.data
        for line in report.read("checksums.txt").decode("ascii").splitlines():
            digest, filename = line.split("  ", 1)
            assert hashlib.sha256(report.read(filename)).hexdigest() == digest


def test_inspection_export_rejects_inspection_issues(client, admin_headers, monkeypatch):
    original_inspect = lab_bootstrap_demo.inspect_archive_analysis

    def report_issue(analysis):
        inspection = original_inspect(analysis)
        inspection["issues"].append("Synthetic validation issue.")
        return inspection

    monkeypatch.setattr(lab_bootstrap_demo, "inspect_archive_analysis", report_issue)
    response = _export_inspection(client, admin_headers, _zip_bytes())

    assert response.status_code == 422
    assert response.get_json() == {
        "error": "export_not_supported",
        "message": "Inspection issues or unsupported state prevent creating a review-only report.",
    }


def test_inspection_export_rejects_invalid_and_unsupported_archives_before_export(
    client, admin_headers, monkeypatch
):
    def fail_if_export_runs(*args, **kwargs):
        raise AssertionError("A rejected archive must not reach the report exporter")

    monkeypatch.setattr(
        lab_bootstrap_demo, "build_safe_inspection_report_package", fail_if_export_runs
    )

    invalid = _export_inspection(client, admin_headers, b"plain text, not a ZIP")
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "invalid_archive"

    unsupported = _export_inspection(
        client, admin_headers, _zip_bytes(payload=b"<unsupported-topology />")
    )
    assert unsupported.status_code == 422
    assert unsupported.get_json()["error"] == "unsupported_schema"

    malformed_xml = _export_inspection(
        client, admin_headers, _zip_bytes(payload=b"<lab><topology></lab>")
    )
    assert malformed_xml.status_code == 400
    assert malformed_xml.get_json()["error"] == "invalid_archive"


def test_inspection_export_requires_jwt(client):
    response = _export_inspection(client, {}, _zip_bytes())

    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_inspection_export_rejects_oversized_body_and_sanitizes_internal_errors(
    client, admin_headers, monkeypatch
):
    monkeypatch.setattr(lab_bootstrap_demo, "MAX_UPLOAD_BYTES", 32)
    oversized = _export_inspection(client, admin_headers, b"x" * 33)
    assert oversized.status_code == 413
    assert oversized.get_json()["error"] == "upload_too_large"

    oversized_without_length = _export_inspection(
        client,
        admin_headers,
        b"x" * 33,
        environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
    )
    assert oversized_without_length.status_code == 413
    assert oversized_without_length.get_json()["error"] == "upload_too_large"

    monkeypatch.setattr(lab_bootstrap_demo, "MAX_UPLOAD_BYTES", 10 * 1024 * 1024)

    def fail_with_internal_path(*args, **kwargs):
        raise RuntimeError("Traceback at D:\\private\\workspace\\parser.py")

    monkeypatch.setattr(lab_bootstrap_demo, "analyze_pnetlab_zip", fail_with_internal_path)
    failed = _export_inspection(client, admin_headers, _zip_bytes())
    assert failed.status_code == 500
    text = failed.get_data(as_text=True)
    assert failed.get_json() == {
        "error": "analysis_failed",
        "message": "The archive could not be analyzed safely.",
    }
    assert "Traceback" not in text
    assert "private" not in text
    assert "parser.py" not in text


def test_inspection_export_sanitizes_report_builder_failure(client, admin_headers, monkeypatch):
    def fail_with_sensitive_detail(*args, **kwargs):
        raise RuntimeError("secret token at D:\\private\\report.py")

    monkeypatch.setattr(
        lab_bootstrap_demo,
        "build_safe_inspection_report_package",
        fail_with_sensitive_detail,
    )
    response = _export_inspection(client, admin_headers, _zip_bytes())

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "export_failed",
        "message": "The review-only report could not be created safely.",
    }
    text = response.get_data(as_text=True)
    assert "secret" not in text
    assert "token" not in text
    assert "Traceback" not in text
    assert "D:\\private" not in text


def test_archive_analysis_route_rate_limit_is_scoped_to_that_endpoint(
    rate_limited_demo_client,
):
    client, headers = rate_limited_demo_client

    responses = [
        client.post(
            "/api/lab-bootstrap-demo/analyze-archive",
            data=b"not parsed because media type is unsupported",
            content_type="application/octet-stream",
            headers=headers,
        )
        for _ in range(6)
    ]

    assert [response.status_code for response in responses] == [415] * 5 + [429]
    assert client.get("/api/health").status_code == 200


def test_inspection_export_rate_limit_is_scoped_to_that_endpoint(rate_limited_demo_client):
    client, headers = rate_limited_demo_client
    responses = [
        client.post(
            "/api/lab-bootstrap-demo/export-inspection",
            data=b"unsupported media type",
            content_type="application/octet-stream",
            headers=headers,
        )
        for _ in range(6)
    ]

    assert [response.status_code for response in responses] == [415] * 5 + [429]
    assert client.get("/api/health").status_code == 200


def test_inspection_export_respects_shared_analysis_semaphore(
    client, admin_headers, monkeypatch
):
    slots = BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(lab_bootstrap_demo, "_archive_analysis_slots", slots)
    monkeypatch.setattr(
        lab_bootstrap_demo,
        "analyze_pnetlab_zip",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("parser must not run")),
    )

    response = _export_inspection(client, admin_headers, _zip_bytes())

    assert response.status_code == 503
    assert response.get_json()["error"] == "analysis_busy"
    slots.release()


def test_archive_analysis_returns_busy_when_the_only_slot_is_occupied(
    client, admin_headers, monkeypatch
):
    slots = BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(lab_bootstrap_demo, "_archive_analysis_slots", slots)

    def fail_if_parser_runs(*args, **kwargs):
        raise AssertionError("A request rejected as busy must not parse the archive")

    monkeypatch.setattr(lab_bootstrap_demo, "analyze_pnetlab_zip", fail_if_parser_runs)
    response = _upload(client, admin_headers, _zip_bytes())

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "analysis_busy",
        "message": "A ZIP analysis is already in progress. Try again shortly.",
    }
    slots.release()


def test_archive_analysis_rejects_a_real_concurrent_request_and_releases_slot(
    client, admin_headers, monkeypatch
):
    first_analysis_entered = Event()
    release_first_analysis = Event()
    request_done = {"first": Event(), "second": Event()}
    results = {}
    thread_errors = {}
    original_analyzer = lab_bootstrap_demo.analyze_pnetlab_zip

    def blocking_analyzer(filename, archive_bytes):
        first_analysis_entered.set()
        if not release_first_analysis.wait(timeout=5):
            raise RuntimeError("test timed out while holding the analysis slot")
        return original_analyzer(filename, archive_bytes)

    monkeypatch.setattr(lab_bootstrap_demo, "analyze_pnetlab_zip", blocking_analyzer)

    def send_request(name):
        try:
            # Each thread owns its Flask test client, as separate WSGI requests
            # would in the threaded Safe Demo server.
            with client.application.test_client() as thread_client:
                results[name] = _upload(
                    thread_client, admin_headers, _zip_bytes()
                )
        except BaseException as exc:  # Capture thread failures for the main test.
            thread_errors[name] = exc
        finally:
            request_done[name].set()

    first = Thread(target=send_request, args=("first",), daemon=True)
    second = Thread(target=send_request, args=("second",), daemon=True)
    try:
        first.start()
        assert first_analysis_entered.wait(timeout=3), "first request did not enter parser"

        # The first request is held inside parser execution, after acquiring the
        # semaphore, before the second request is started.
        second.start()
        assert request_done["second"].wait(timeout=3), "second request did not finish"
        assert "second" not in thread_errors
        assert results["second"].status_code == 503
        assert results["second"].get_json()["error"] == "analysis_busy"
        assert not request_done["first"].is_set()
    finally:
        release_first_analysis.set()
        if first.ident is not None:
            first.join(timeout=3)
        if second.ident is not None:
            second.join(timeout=3)

    assert not first.is_alive(), "first request thread did not terminate"
    assert not second.is_alive(), "second request thread did not terminate"
    assert "first" not in thread_errors
    assert results["first"].status_code == 200

    # Once the first request has returned, a fresh request must acquire the slot.
    next_request = _upload(client, admin_headers, _zip_bytes())
    assert next_request.status_code == 200


def test_archive_analysis_slot_is_released_after_error_and_next_request_succeeds(
    client, admin_headers, monkeypatch
):
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(lab_bootstrap_demo, "_archive_analysis_slots", slots)
    original_analyzer = lab_bootstrap_demo.analyze_pnetlab_zip
    calls = 0

    def fail_once_then_analyze(filename, archive_bytes):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic parser failure")
        return original_analyzer(filename, archive_bytes)

    monkeypatch.setattr(lab_bootstrap_demo, "analyze_pnetlab_zip", fail_once_then_analyze)

    failed = _upload(client, admin_headers, _zip_bytes())
    assert failed.status_code == 500
    assert slots.acquire(blocking=False)
    slots.release()

    succeeded = _upload(client, admin_headers, _zip_bytes())
    assert succeeded.status_code == 200
    assert succeeded.get_json()["planner_status"] == "NOT_RUN"
    assert slots.acquire(blocking=False)
    slots.release()


def test_invalid_zip_returns_safe_error_without_stack_or_path(client, admin_headers):
    response = _upload(client, admin_headers, b"not a zip archive")

    assert response.status_code == 400
    text = response.get_data(as_text=True)
    assert "valid, readable ZIP" in text
    assert "Traceback" not in text
    assert "D:\\" not in text
    assert "pnetlab_archive.py" not in text


def test_readable_zip_with_unsupported_unl_schema_has_distinct_safe_error(client, admin_headers):
    response = _upload(
        client,
        admin_headers,
        _zip_bytes(payload=b"<unsupported-topology />"),
    )

    assert response.status_code == 422
    assert response.get_json() == {
        "error": "unsupported_schema",
        "message": "The .unl XML root must be <lab>.",
    }
    assert "Traceback" not in response.get_data(as_text=True)
    assert "pnetlab_archive.py" not in response.get_data(as_text=True)


def test_well_formed_topology_missing_nodes_has_incomplete_topology_error(client, admin_headers):
    response = _upload(
        client,
        admin_headers,
        _zip_bytes(payload=b"<lab><topology><nodes /><networks /></topology></lab>"),
    )

    assert response.status_code == 422
    assert response.get_json() == {
        "error": "incomplete_topology",
        "message": "The .unl topology does not contain any nodes.",
    }


def test_unexpected_analysis_error_is_sanitized(client, admin_headers, monkeypatch):
    def fail_with_internal_path(*args, **kwargs):
        raise RuntimeError("Traceback at D:\\private\\workspace\\parser.py")

    monkeypatch.setattr(lab_bootstrap_demo, "analyze_pnetlab_zip", fail_with_internal_path)

    response = _upload(client, admin_headers, _zip_bytes())

    assert response.status_code == 500
    text = response.get_data(as_text=True)
    assert response.get_json()["message"] == "The archive could not be analyzed safely."
    assert "Traceback" not in text
    assert "private" not in text
    assert "parser.py" not in text


def test_upload_size_limit_returns_413(client, admin_headers, monkeypatch):
    monkeypatch.setattr(lab_bootstrap_demo, "MAX_UPLOAD_BYTES", 32)

    response = _upload(client, admin_headers, b"x" * 33)

    assert response.status_code == 413
    assert response.get_json()["error"] == "upload_too_large"


def test_actual_body_size_limit_returns_413_without_content_length(
    client, admin_headers, monkeypatch
):
    monkeypatch.setattr(lab_bootstrap_demo, "MAX_UPLOAD_BYTES", 32)

    response = _upload(
        client,
        admin_headers,
        b"x" * 33,
        environ_overrides={
            "CONTENT_LENGTH": "",
            "wsgi.input_terminated": True,
        },
    )

    assert response.status_code == 413
    assert response.get_json()["error"] == "upload_too_large"


@pytest.mark.parametrize("content_length", [None, "1"])
def test_upload_with_missing_or_false_content_length_fails_closed(
    client, admin_headers, content_length
):
    environ_overrides = {"CONTENT_LENGTH": "" if content_length is None else content_length}
    response = _upload(
        client,
        admin_headers,
        _zip_bytes(),
        environ_overrides=environ_overrides,
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_archive"


def test_path_traversal_member_is_rejected(client, admin_headers):
    response = _upload(client, admin_headers, _zip_bytes("../outside.unl"))

    assert response.status_code == 400
    assert "unsafe member path" in response.get_json()["message"]


def test_client_cannot_submit_inspection_or_topology_instead_of_zip(client, admin_headers):
    response = client.post(
        "/api/lab-bootstrap-demo/analyze-archive",
        json={
            "planner_eligible": True,
            "preview_compatible": True,
            "status": "ANALYZED_VERIFIED_SCHEMA",
            "topology": [{"kind": "network"}],
        },
        headers=admin_headers,
    )

    assert response.status_code == 415
    assert response.get_json()["error"] == "unsupported_media_type"


def test_client_headers_cannot_override_archive_inspection(client, admin_headers):
    response = client.post(
        "/api/lab-bootstrap-demo/analyze-archive",
        data=_zip_bytes(),
        content_type="application/zip",
        headers={
            **admin_headers,
            "X-Planner-Eligible": "true",
            "X-Preview-Compatible": "true",
            "X-Archive-Status": "ANALYZED_VERIFIED_SCHEMA",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert body["preview_compatible"] is False
    assert body["planner_eligible"] is False


def test_archive_api_requires_existing_jwt(client):
    response = client.post(
        "/api/lab-bootstrap-demo/analyze-archive",
        data=_zip_bytes(),
        content_type="application/zip",
    )

    assert response.status_code == 401
    assert "Authentication is required" in response.get_json()["message"]


def test_archive_api_rejects_invalid_jwt(client):
    response = client.post(
        "/api/lab-bootstrap-demo/analyze-archive",
        data=_zip_bytes(),
        content_type="application/zip",
        headers={"Authorization": "Bearer invalid.token.value"},
    )

    assert response.status_code == 401
    assert response.get_json()["message"] == "The access token is invalid."


def test_archive_api_rejects_expired_jwt(client, app):
    with app.app_context():
        token = create_access_token(
            identity="safe-demo-test-user",
            expires_delta=timedelta(seconds=-1),
        )

    response = client.post(
        "/api/lab-bootstrap-demo/analyze-archive",
        data=_zip_bytes(),
        content_type="application/zip",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.get_json()["message"] == "The access token has expired."
