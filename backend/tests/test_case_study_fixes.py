"""Fixes for problems met while running the report's case study (Nov 2026)."""

from flask import g
from types import SimpleNamespace

from network_copilot.ai.language import ui_text, with_reply_language
from network_copilot.governance.redaction import redact_output
from network_copilot.topology import chat_import


def test_hostname_drops_the_kind_in_brackets():
    used: set[str] = set()
    assert chat_import._hostname("R1 (Router)", "AI-ROUTER-1", used) == "R1"
    assert chat_import._hostname("SW1 (Switch)", "AI-SWITCH-1", used) == "SW1"
    assert chat_import._hostname("PCs (30)", "AI-HOST-1", used) == "PCS"
    # A second "R1 (Router)" still gets a unique name.
    assert chat_import._hostname("R1 (Router)", "AI-ROUTER-2", used) == "R1-2"


def test_hostname_keeps_labels_without_brackets():
    assert chat_import._hostname("Core Switch", "X", set()) == "CORE-SWITCH"
    assert chat_import._hostname("(Router)", "AI-ROUTER-1", set()) == "ROUTER"


def _link():
    return SimpleNamespace(label="Uplink", network="10.100.0.0/24",
                           ip_source="10.100.0.1/24", ip_target="10.100.0.254/24")


def test_link_description_follows_the_page_language(app):
    with app.test_request_context(headers={"X-UI-Language": "en"}):
        app.preprocess_request()
        text = chat_import._link_description(_link())
    assert text == "Uplink · Network: 10.100.0.0/24 · Source IP: 10.100.0.1/24 · Target IP: 10.100.0.254/24"
    g.pop("ui_language", None)  # the test app context outlives one request
    with app.test_request_context():
        app.preprocess_request()
        assert "IP nguồn: 10.100.0.1/24" in chat_import._link_description(_link())


def test_language_header_sets_ui_language(app):
    with app.test_request_context(headers={"X-UI-Language": "EN"}):
        app.preprocess_request()
        assert g.ui_language == "en"
        assert ui_text("Mạng", "Network") == "Network"
    g.pop("ui_language", None)  # the test app context outlives one request
    with app.test_request_context(headers={"X-UI-Language": "fr"}):
        app.preprocess_request()
        assert g.get("ui_language") is None


def test_reply_language_is_added_to_the_prompt_only_when_known(app):
    g.pop("ui_language", None)
    with app.test_request_context():
        assert with_reply_language("Base prompt.") == "Base prompt."
        g.ui_language = "en"
        assert "in English" in with_reply_language("Base prompt.")
        g.ui_language = "vi"
        assert "in Vietnamese" in with_reply_language("Base prompt.")


def test_ai_provider_sends_the_language_rule(app, monkeypatch):
    from network_copilot.ai import provider as provider_module

    sent = {}

    class FakeGemini(provider_module.GeminiProvider):
        def __init__(self):
            pass

        def _call(self, contents, config):
            sent.update(config)
            return SimpleNamespace(text="{}")

    with app.test_request_context(headers={"X-UI-Language": "en"}):
        app.preprocess_request()
        FakeGemini()._generate("Tutor prompt.", "hello", {}, json_mode=False)
    assert sent["system_instruction"].startswith("Tutor prompt.")
    assert "in English" in sent["system_instruction"]


def test_prose_heading_is_not_masked_as_a_credential():
    answer = "Here is a breakdown.\n\n### Key Concepts:\n- Router R1 is the gateway."
    assert redact_output(answer) == answer
    assert "Key points" in redact_output("Key points to remember")


def test_real_credentials_are_still_masked():
    config = "enable secret 5 $1$abcd$xyz\nusername admin password cisco123\nsnmp-server community public RO"
    masked = redact_output(config)
    assert "cisco123" not in masked and "public" not in masked and "$1$abcd" not in masked
    mixed = "### Key Concepts:\npassword cisco123"
    masked = redact_output(mixed)
    assert "### Key Concepts:" in masked and "cisco123" not in masked
