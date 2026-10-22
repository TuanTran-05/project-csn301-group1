import io
import logging
import zipfile
from datetime import timedelta

import jwt as pyjwt
import pytest
from flask_jwt_extended import create_access_token

from network_copilot import lab_bootstrap_demo_runner as runner
from network_copilot import lab_bootstrap_demo
from network_copilot.config import TestConfig


def test_safe_demo_runner_uses_in_memory_config_with_limiter_enabled():
    assert issubclass(runner.SafeDemoConfig, TestConfig)
    assert runner.SafeDemoConfig.SQLALCHEMY_DATABASE_URI == "sqlite:///:memory:"
    assert runner.SafeDemoConfig.MONITORING_ENABLED is False
    assert runner.SafeDemoConfig.SECRET_KEY is None
    assert runner.SafeDemoConfig.JWT_SECRET_KEY is None
    assert runner.SafeDemoConfig.JWT_TOKEN_LOCATION == ("cookies",)
    assert runner.SafeDemoConfig.JWT_COOKIE_CSRF_PROTECT is True
    assert runner.SafeDemoConfig.JWT_COOKIE_SAMESITE == "Strict"
    assert runner.SafeDemoConfig.JWT_DECODE_AUDIENCE == "lab-bootstrap-demo"
    assert runner.SafeDemoConfig.JWT_DECODE_ISSUER == "lab-bootstrap-safe-demo"
    assert runner.SafeDemoConfig.RATELIMIT_ENABLED is True
    assert runner.SafeDemoConfig.RATELIMIT_STORAGE_URI == "memory://"
    assert runner.SafeDemoConfig.JWT_ACCESS_TOKEN_EXPIRES == timedelta(minutes=5)


def test_safe_demo_runner_generates_new_ephemeral_keys_per_app(monkeypatch):
    generated = []

    def deterministic_test_key(byte_count):
        generated.append(byte_count)
        return f"test-only-ephemeral-{len(generated)}"

    monkeypatch.setattr(runner.secrets, "token_urlsafe", deterministic_test_key)
    first = runner.create_safe_demo_app()
    second = runner.create_safe_demo_app()

    assert generated == [32, 32, 32, 32]
    assert first.config["JWT_SECRET_KEY"] == "test-only-ephemeral-2"
    assert second.config["JWT_SECRET_KEY"] == "test-only-ephemeral-4"
    assert first.config["JWT_SECRET_KEY"] != second.config["JWT_SECRET_KEY"]


def test_safe_demo_runner_requests_single_process_loopback_bind(monkeypatch):
    captured = {}

    class FakeApp:
        def run(self, **kwargs):
            captured["run_kwargs"] = kwargs

    def fake_create_safe_demo_app():
        return FakeApp()

    monkeypatch.setattr(runner, "create_safe_demo_app", fake_create_safe_demo_app)
    runner.main()

    assert captured["run_kwargs"] == {
        "host": "127.0.0.1",
        "port": runner.SAFE_DEMO_PORT,
        "debug": False,
        "use_reloader": False,
        "threaded": True,
        "processes": 1,
    }


def _safe_zip_bytes():
    xml = b"""<lab name='runner test'><topology><nodes>
    <node id='1' name='router1' template='vios' image='image-a'>
    <interface id='0' name='Gi0/0' network_id='1'/></node>
    </nodes><networks><network id='1' name='net1' type='bridge'/></networks>
    </topology></lab>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("topology/test.unl", xml)
    return output.getvalue()


def _is_absent(container, value):
    return value not in container


_AUTO_ORIGIN = object()


def _runner_request(
    client,
    method,
    path,
    *,
    host=runner.SAFE_DEMO_AUTHORITY,
    origin=_AUTO_ORIGIN,
    sec_fetch_site=_AUTO_ORIGIN,
    **kwargs,
):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Host"] = host
    if origin is _AUTO_ORIGIN:
        if method.upper() == "POST" and path in runner.SAFE_DEMO_POST_PATHS:
            headers["Origin"] = runner.SAFE_DEMO_ORIGIN
    elif origin is not None:
        headers["Origin"] = origin
    if sec_fetch_site is not _AUTO_ORIGIN and sec_fetch_site is not None:
        headers["Sec-Fetch-Site"] = sec_fetch_site
    return client.open(
        path,
        method=method,
        base_url=runner.SAFE_DEMO_ORIGIN,
        headers=headers,
        **kwargs,
    )


def _cookie_authenticated_runner_client():
    app = runner.create_safe_demo_app()
    client = app.test_client()
    page_response = _runner_request(client, "GET", "/lab-bootstrap-demo")
    assert page_response.status_code == 200
    csrf_cookie = client.get_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )
    assert csrf_cookie is not None
    return app, client, csrf_cookie.value


def test_safe_demo_runner_registers_only_required_routes_and_accepts_jwt(caplog):
    app = runner.create_safe_demo_app()
    rules = {
        (rule.rule, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
        for rule in app.url_map.iter_rules()
    }

    assert rules == {
        ("/lab-bootstrap-demo", ("GET",)),
        (runner.SAFE_DEMO_SCRIPT_PATH, ("GET",)),
        ("/api/lab-bootstrap-demo/analyze-archive", ("POST",)),
        (runner.SAFE_DEMO_EXPORT_PATH, ("POST",)),
    }

    client = app.test_client()
    with caplog.at_level(logging.DEBUG):
        page_response = _runner_request(client, "GET", "/lab-bootstrap-demo")
    assert page_response.status_code == 200
    assert b"JWT access token" not in page_response.data
    assert page_response.headers["Cache-Control"] == "no-store"
    access_cookie = client.get_cookie(
        runner.SAFE_DEMO_ACCESS_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/api/lab-bootstrap-demo/",
    )
    csrf_cookie = client.get_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )
    assert access_cookie is not None
    assert access_cookie.http_only is True
    assert access_cookie.path == "/api/lab-bootstrap-demo/"
    assert access_cookie.same_site == "Strict"
    assert access_cookie.max_age == runner.SAFE_DEMO_TOKEN_MAX_AGE_SECONDS
    assert csrf_cookie is not None
    assert csrf_cookie.http_only is False
    assert csrf_cookie.path == "/lab-bootstrap-demo"
    assert _is_absent(page_response.get_data(), access_cookie.value.encode())
    assert _is_absent(caplog.text, access_cookie.value)
    assert _is_absent(caplog.text, app.config["JWT_SECRET_KEY"])
    assert _runner_request(client, "GET", runner.SAFE_DEMO_SCRIPT_PATH).status_code == 200
    for path in ("/", "/dashboard", "/api/health", "/api/devices", "/api/auth/login"):
        assert _runner_request(client, "GET", path).status_code == 404

    unauthenticated = _runner_request(
        app.test_client(),
        "POST",
        "/api/lab-bootstrap-demo/analyze-archive",
        data=_safe_zip_bytes(),
        content_type="application/zip",
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.get_json()["error"] == "unauthorized"

    with caplog.at_level(logging.DEBUG):
        authenticated = _runner_request(
            client,
            "POST",
            "/api/lab-bootstrap-demo/analyze-archive",
            data=_safe_zip_bytes(),
            content_type="application/zip",
            headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_cookie.value},
            sec_fetch_site="same-origin",
        )
    assert authenticated.status_code == 200
    assert authenticated.get_json()["status"] == "ANALYZED_UNVERIFIED_SCHEMA"
    assert _is_absent(authenticated.get_data(), access_cookie.value.encode())
    assert _is_absent(caplog.text, access_cookie.value)
    assert _is_absent(caplog.text, app.config["JWT_SECRET_KEY"])

    missing_csrf = _runner_request(
        client,
        "POST",
        "/api/lab-bootstrap-demo/analyze-archive",
        data=_safe_zip_bytes(),
        content_type="application/zip",
    )
    assert missing_csrf.status_code == 401

    wrong_csrf = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_ANALYZE_PATH,
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: "incorrect-csrf-token"},
        sec_fetch_site="same-origin",
    )
    assert wrong_csrf.status_code == 401


def test_cookie_authenticated_runner_rejects_invalid_zip_safely():
    _app, client, csrf_token = _cookie_authenticated_runner_client()

    response = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_ANALYZE_PATH,
        data=b"plain text, not a ZIP archive",
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_token},
        sec_fetch_site="same-origin",
        environ_overrides={"REMOTE_ADDR": "127.0.0.2"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_archive"
    body = response.get_data(as_text=True)
    assert "valid, readable ZIP" in body
    assert "Traceback" not in body
    assert "pnetlab_archive.py" not in body


def test_cookie_authenticated_runner_rejects_oversized_body_without_content_length(
    monkeypatch,
):
    monkeypatch.setattr(lab_bootstrap_demo, "MAX_UPLOAD_BYTES", 32)
    _app, client, csrf_token = _cookie_authenticated_runner_client()

    response = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_ANALYZE_PATH,
        data=b"x" * 33,
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_token},
        sec_fetch_site="same-origin",
        environ_overrides={
            "CONTENT_LENGTH": "",
            "wsgi.input_terminated": True,
            "REMOTE_ADDR": "127.0.0.3",
        },
    )

    assert response.status_code == 413
    assert response.get_json()["error"] == "upload_too_large"


def test_cookie_authenticated_runner_checks_body_when_content_length_is_too_small(
    monkeypatch,
):
    monkeypatch.setattr(lab_bootstrap_demo, "MAX_UPLOAD_BYTES", 32)
    _app, client, csrf_token = _cookie_authenticated_runner_client()

    response = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_ANALYZE_PATH,
        data=b"x" * 33,
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_token},
        sec_fetch_site="same-origin",
        environ_overrides={
            "CONTENT_LENGTH": "1",
            "wsgi.input_terminated": True,
            "REMOTE_ADDR": "127.0.0.4",
        },
    )

    assert response.status_code == 413
    assert response.get_json()["error"] == "upload_too_large"


def test_runner_accepts_the_expected_loopback_host():
    app = runner.create_safe_demo_app()
    response = _runner_request(app.test_client(), "GET", "/lab-bootstrap-demo")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "localhost:5058",
        "example.test:5058",
        "127.0.0.1",
        "127.0.0.1:5059",
        "127.0.0.1:5058.evil.test",
        "127.0.0.1:5058,example.test:5058",
        "[::1]:5058",
        "",
    ],
)
def test_runner_rejects_untrusted_or_malformed_host(host):
    app = runner.create_safe_demo_app()
    response = _runner_request(
        app.test_client(),
        "GET",
        "/lab-bootstrap-demo",
        host=host,
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "host_not_allowed"


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5058",
        "https://127.0.0.1:5058",
        "http://127.0.0.1",
        "http://evil.test:5058",
        "null",
    ],
)
def test_archive_api_rejects_cross_origin_with_valid_cookie_and_csrf(origin):
    app = runner.create_safe_demo_app()
    client = app.test_client()
    page_response = _runner_request(client, "GET", "/lab-bootstrap-demo")
    assert page_response.status_code == 200
    csrf_cookie = client.get_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )
    assert csrf_cookie is not None

    response = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_ANALYZE_PATH,
        origin=origin,
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_cookie.value},
        sec_fetch_site="same-origin",
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "origin_not_allowed"


def test_archive_api_rejects_missing_origin_even_with_valid_cookie_and_csrf():
    app = runner.create_safe_demo_app()
    client = app.test_client()
    _runner_request(client, "GET", "/lab-bootstrap-demo")
    csrf_cookie = client.get_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )
    assert csrf_cookie is not None

    response = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_ANALYZE_PATH,
        origin=None,
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_cookie.value},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "origin_not_allowed"


def test_inspection_export_requires_same_origin_cookie_and_csrf():
    app = runner.create_safe_demo_app()
    client = app.test_client()
    _runner_request(client, "GET", "/lab-bootstrap-demo")
    csrf_cookie = client.get_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )
    assert csrf_cookie is not None

    missing_csrf = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_EXPORT_PATH,
        data=_safe_zip_bytes(),
        content_type="application/zip",
    )
    assert missing_csrf.status_code == 401

    exported = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_EXPORT_PATH,
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_cookie.value},
        sec_fetch_site="same-origin",
    )
    assert exported.status_code == 200
    assert exported.mimetype == "application/zip"
    assert "lab-bootstrap-inspection-report.zip" in exported.headers["Content-Disposition"]

    invalid_csrf = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_EXPORT_PATH,
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: "not-the-cookie-token"},
        sec_fetch_site="same-origin",
    )
    assert invalid_csrf.status_code == 401


@pytest.mark.parametrize("origin", [None, "http://evil.test:5058"])
def test_inspection_export_rejects_missing_or_cross_origin(origin):
    app = runner.create_safe_demo_app()
    client = app.test_client()
    _runner_request(client, "GET", "/lab-bootstrap-demo")
    csrf_cookie = client.get_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )
    assert csrf_cookie is not None

    response = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_EXPORT_PATH,
        origin=origin,
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_cookie.value},
        sec_fetch_site="same-origin",
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "origin_not_allowed"


@pytest.mark.parametrize("fetch_site", ["cross-site", "same-site", "none", "SAME-ORIGIN"])
def test_archive_api_rejects_noncanonical_fetch_metadata_with_valid_origin(fetch_site):
    app = runner.create_safe_demo_app()
    client = app.test_client()
    _runner_request(client, "GET", "/lab-bootstrap-demo")
    csrf_cookie = client.get_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )
    assert csrf_cookie is not None

    response = _runner_request(
        client,
        "POST",
        runner.SAFE_DEMO_ANALYZE_PATH,
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_cookie.value},
        sec_fetch_site=fetch_site,
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "origin_not_allowed"


def _set_cookie_credentials(client, access_token, csrf_token):
    client.set_cookie(
        runner.SAFE_DEMO_ACCESS_COOKIE_NAME,
        access_token,
        domain=runner.SAFE_DEMO_HOST,
        path="/api/lab-bootstrap-demo/",
    )
    client.set_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        csrf_token,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )


def test_token_from_previous_runner_instance_is_rejected():
    previous_app = runner.create_safe_demo_app()
    previous_client = previous_app.test_client()
    assert _runner_request(previous_client, "GET", "/lab-bootstrap-demo").status_code == 200
    old_access = previous_client.get_cookie(
        runner.SAFE_DEMO_ACCESS_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/api/lab-bootstrap-demo/",
    )
    old_csrf = previous_client.get_cookie(
        runner.SAFE_DEMO_CSRF_COOKIE_NAME,
        domain=runner.SAFE_DEMO_HOST,
        path="/lab-bootstrap-demo",
    )
    assert old_access is not None and old_csrf is not None

    restarted_app = runner.create_safe_demo_app()
    restarted_client = restarted_app.test_client()
    _set_cookie_credentials(restarted_client, old_access.value, old_csrf.value)
    response = _runner_request(
        restarted_client,
        "POST",
        "/api/lab-bootstrap-demo/analyze-archive",
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: old_csrf.value},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_expired_safe_demo_cookie_is_rejected():
    app = runner.create_safe_demo_app()
    with app.app_context():
        expired_token = create_access_token(
            identity="safe-demo",
            additional_claims={"demo_only": True},
            expires_delta=timedelta(seconds=-1),
        )
        csrf_token = pyjwt.decode(
            expired_token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )["csrf"]

    client = app.test_client()
    _set_cookie_credentials(client, expired_token, csrf_token)
    response = _runner_request(
        client,
        "POST",
        "/api/lab-bootstrap-demo/analyze-archive",
        data=_safe_zip_bytes(),
        content_type="application/zip",
        headers={runner.SAFE_DEMO_CSRF_HEADER_NAME: csrf_token},
    )

    assert response.status_code == 401
    assert response.get_json()["message"] == "The access token has expired."
