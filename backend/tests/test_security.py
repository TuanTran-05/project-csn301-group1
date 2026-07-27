import pytest
from fakes.fake_ai_provider import FakeAIProvider

from network_copilot.app import create_app
from network_copilot.config import TestConfig
from network_copilot.extensions import db as _db


# -- security headers -----------------------------------------------------


@pytest.mark.parametrize(
    "header,value",
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cache-Control", "no-store"),
    ],
)
def test_security_headers_are_present(client, header, value):
    response = client.get("/api/health")
    assert response.headers[header] == value


def test_security_headers_are_present_on_errors(client):
    response = client.get("/api/devices")
    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"


# -- request id -----------------------------------------------------------


def test_every_response_carries_a_request_id(client):
    response = client.get("/api/health")
    assert response.headers.get("X-Request-ID")


def test_a_supplied_request_id_is_echoed(client):
    response = client.get("/api/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


def test_request_ids_are_unique(client):
    first = client.get("/api/health").headers["X-Request-ID"]
    second = client.get("/api/health").headers["X-Request-ID"]
    assert first != second


# -- JSON error contract --------------------------------------------------


def test_404_returns_json(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.is_json
    assert response.get_json()["error"] == "not_found"


def test_405_returns_json(client):
    response = client.delete("/api/health")
    assert response.status_code == 405
    assert response.is_json
    assert response.get_json()["error"] == "method_not_allowed"


def test_401_returns_json(client):
    response = client.get("/api/devices")
    assert response.is_json
    assert "message" in response.get_json()


def test_validation_error_returns_json_details(client, admin_headers):
    response = client.post(
        "/api/devices", headers=admin_headers, json={"hostname": "bad host"}
    )
    assert response.status_code == 422
    body = response.get_json()
    assert body["error"] == "validation_error"
    assert "details" in body


def test_error_responses_include_the_request_id(client):
    response = client.get("/api/does-not-exist")
    assert response.get_json()["request_id"] == response.headers["X-Request-ID"]


# -- unhandled exceptions -------------------------------------------------


def _app_with_boom():
    app = create_app(TestConfig)

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("db password is hunter2 and the token is abc123")

    return app


def test_unhandled_error_returns_a_generic_json_500():
    app = _app_with_boom()
    with app.app_context():
        _db.create_all()
        client = app.test_client()
        response = client.get("/api/boom")

        assert response.status_code == 500
        assert response.is_json
        body = response.get_json()
        assert body["error"] == "internal_error"
        assert body["message"] == "An internal error occurred."


def test_unhandled_error_never_leaks_secrets_or_tracebacks():
    app = _app_with_boom()
    with app.app_context():
        _db.create_all()
        body = app.test_client().get("/api/boom").get_data(as_text=True)

        assert "hunter2" not in body
        assert "abc123" not in body
        assert "Traceback" not in body
        assert "RuntimeError" not in body
        assert ".py" not in body


# -- rate limiting --------------------------------------------------------


class RateLimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True


@pytest.fixture
def limited_app():
    app = create_app(RateLimitedConfig)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def test_login_is_rate_limited_to_five_per_minute(limited_app):
    client = limited_app.test_client()
    payload = {"username": "admin", "password": "wrong"}

    statuses = [
        client.post("/api/auth/login", json=payload).status_code for _ in range(6)
    ]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


def test_rate_limited_response_is_json(limited_app):
    client = limited_app.test_client()
    payload = {"username": "admin", "password": "wrong"}
    for _ in range(6):
        response = client.post("/api/auth/login", json=payload)

    assert response.status_code == 429
    assert response.is_json
    assert response.get_json()["error"] == "rate_limit_exceeded"


def test_ai_chat_is_rate_limited_to_twenty_per_minute(limited_app):
    from network_copilot.auth.model import User

    with limited_app.app_context():
        user = User(username="admin", role="ADMIN")
        user.set_password("StrongPass123!")
        _db.session.add(user)
        _db.session.commit()

    limited_app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(
        responses={"nonsense": True}
    )
    client = limited_app.test_client()
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "StrongPass123!"}
    ).get_json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    statuses = [
        client.post("/api/ai/chat", headers=headers, json={"message": "hi"}).status_code
        for _ in range(21)
    ]
    assert statuses[:20] == [422] * 20
    assert statuses[20] == 429


def test_rate_limiting_is_disabled_in_the_default_test_config(client):
    payload = {"username": "admin", "password": "wrong"}
    statuses = [
        client.post("/api/auth/login", json=payload).status_code for _ in range(8)
    ]
    assert 429 not in statuses
