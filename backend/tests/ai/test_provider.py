import pytest
from fakes.fake_ai_provider import FakeAIProvider

from network_copilot.ai.provider import (
    AnthropicProvider,
    GeminiProvider,
    NullProvider,
    build_provider,
)
from network_copilot.errors import AIProviderError, AIProviderNotConfiguredError


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, text='{"ok": true}', raise_with=None):
        self.text = text
        self.raise_with = raise_with
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.raise_with is not None:
            raise self.raise_with
        return FakeResponse(self.text)


class FakeGenaiClient:
    def __init__(self, text='{"ok": true}', raise_with=None):
        self.models = FakeModels(text, raise_with)


def gemini_with(client: FakeGenaiClient, model="gemini-2.5-flash") -> GeminiProvider:
    return GeminiProvider("test-key", model, client_factory=lambda: client)


# -- provider selection ---------------------------------------------------


def test_test_override_wins(app):
    sentinel = FakeAIProvider(responses={})
    app.config["AI_PROVIDER_INSTANCE"] = sentinel
    with app.test_request_context():
        assert build_provider() is sentinel


def test_gemini_is_selected_when_configured(app):
    app.config["AI_PROVIDER"] = "gemini"
    app.config["AI_API_KEY"] = "test-key"
    app.config["AI_MODEL"] = "gemini-2.5-flash"
    with app.test_request_context():
        provider = build_provider()
    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-2.5-flash"


def test_google_is_an_alias_for_gemini(app):
    app.config["AI_PROVIDER"] = "google"
    app.config["AI_API_KEY"] = "test-key"
    with app.test_request_context():
        assert isinstance(build_provider(), GeminiProvider)


def test_anthropic_is_still_selectable(app):
    app.config["AI_PROVIDER"] = "anthropic"
    app.config["AI_API_KEY"] = "test-key"
    with app.test_request_context():
        assert isinstance(build_provider(), AnthropicProvider)


def test_missing_api_key_falls_back_to_null_provider(app):
    app.config["AI_PROVIDER"] = "gemini"
    app.config["AI_API_KEY"] = None
    with app.test_request_context():
        assert isinstance(build_provider(), NullProvider)


def test_unknown_provider_name_falls_back_to_null_provider(app):
    app.config["AI_PROVIDER"] = "some-other-llm"
    app.config["AI_API_KEY"] = "test-key"
    with app.test_request_context():
        assert isinstance(build_provider(), NullProvider)


# -- NullProvider reports misconfiguration, not a crash -------------------


def test_null_provider_raises_a_configuration_error():
    with pytest.raises(AIProviderNotConfiguredError):
        NullProvider().complete("system", "message", {})


def test_null_provider_explain_raises_the_same_error():
    with pytest.raises(AIProviderNotConfiguredError):
        NullProvider().explain("system", "message", {})


def test_configuration_error_is_a_503_not_a_500():
    error = AIProviderNotConfiguredError("nope")
    assert error.status_code == 503
    assert error.error == "ai_not_configured"


def test_chat_endpoint_reports_missing_configuration_clearly(
    client, admin_headers, app, dist_switch
):
    app.config["AI_PROVIDER_INSTANCE"] = None
    app.config["AI_API_KEY"] = None

    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "kiem tra ospf"}
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"] == "ai_not_configured"
    assert "AI_API_KEY" in body["message"]


# -- GeminiProvider -------------------------------------------------------


def test_gemini_requires_an_api_key():
    with pytest.raises(ValueError):
        GeminiProvider("", "gemini-2.5-flash")


def test_gemini_complete_returns_the_model_text():
    fake = FakeGenaiClient(text='{"intent": "monitor"}')
    assert gemini_with(fake).complete("sys", "hi", {"devices": []}) == (
        '{"intent": "monitor"}'
    )


def test_gemini_complete_forces_json_output():
    fake = FakeGenaiClient()
    gemini_with(fake).complete("sys", "hi", {"devices": []})
    config = fake.models.calls[0]["config"]
    assert config["response_mime_type"] == "application/json"


def test_gemini_complete_passes_the_system_prompt_as_system_instruction():
    fake = FakeGenaiClient()
    gemini_with(fake).complete("SYSTEM RULES", "hi", {"devices": []})
    assert fake.models.calls[0]["config"]["system_instruction"] == "SYSTEM RULES"


def test_gemini_complete_sends_the_model_name():
    fake = FakeGenaiClient()
    gemini_with(fake, model="gemini-2.5-flash-lite").complete("sys", "hi", {})
    assert fake.models.calls[0]["model"] == "gemini-2.5-flash-lite"


def test_gemini_complete_includes_the_context_and_message():
    fake = FakeGenaiClient()
    gemini_with(fake).complete("sys", "Kiem tra OSPF", {"devices": [{"h": "A"}]})
    contents = fake.models.calls[0]["contents"]
    assert "Kiem tra OSPF" in contents
    assert "devices" in contents


def test_gemini_explain_does_not_force_json():
    fake = FakeGenaiClient(text="OSPF looks healthy.")
    result = gemini_with(fake).explain("sys", "why", {"diagnostics": []})
    assert result == "OSPF looks healthy."
    assert "response_mime_type" not in fake.models.calls[0]["config"]


def test_gemini_uses_a_deterministic_temperature():
    fake = FakeGenaiClient()
    gemini_with(fake).complete("sys", "hi", {})
    assert fake.models.calls[0]["config"]["temperature"] == 0


def test_gemini_handles_an_empty_response():
    fake = FakeGenaiClient(text=None)
    assert gemini_with(fake).complete("sys", "hi", {}) == ""


def test_gemini_reuses_one_client():
    fake = FakeGenaiClient()
    provider = gemini_with(fake)
    provider.complete("sys", "a", {})
    provider.complete("sys", "b", {})
    assert len(fake.models.calls) == 2


def test_gemini_api_failure_becomes_a_provider_error():
    fake = FakeGenaiClient(raise_with=RuntimeError("429 quota exceeded"))
    with pytest.raises(AIProviderError):
        gemini_with(fake).complete("sys", "hi", {})


def test_provider_error_is_a_502_not_a_500():
    error = AIProviderError("upstream died")
    assert error.status_code == 502
    assert error.error == "ai_provider_error"


def test_gemini_provider_error_does_not_leak_the_api_key():
    fake = FakeGenaiClient(raise_with=RuntimeError("bad key: test-key"))
    with pytest.raises(AIProviderError) as exc:
        gemini_with(fake).complete("sys", "hi", {})
    assert "test-key" not in str(exc.value)


def test_chat_endpoint_surfaces_provider_failure_as_502(
    client, admin_headers, app, dist_switch
):
    class BrokenProvider:
        def complete(self, system_prompt, user_message, context):
            raise AIProviderError("The AI provider is unavailable.")

        def explain(self, system_prompt, user_message, context):
            raise AIProviderError("The AI provider is unavailable.")

    app.config["AI_PROVIDER_INSTANCE"] = BrokenProvider()
    response = client.post(
        "/api/ai/chat", headers=admin_headers, json={"message": "kiem tra ospf"}
    )
    assert response.status_code == 502
    assert response.get_json()["error"] == "ai_provider_error"
