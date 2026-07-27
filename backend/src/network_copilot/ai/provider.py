"""AI provider adapters.

A provider only turns text into text. It never receives credentials, never
opens an SSH session and never decides whether something may run.

Gemini is the default: Flash-class models are cheap enough to run a lab copilot
continuously. Anthropic is kept as an alternative.
"""

import json
import logging
from typing import Protocol

from ..errors import AIProviderError, AIProviderNotConfiguredError

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2048
# The model must choose between a small set of known commands, not be creative.
DEFAULT_TEMPERATURE = 0

# Thinking models spend output budget on reasoning before answering. For
# picking one command off an allowlist that buys nothing: measured against
# gemini-3.5-flash, leaving it on made 1 in 4 responses unparseable and cost
# 190-315 extra tokens per call. Disabled, the answer is identical every time.
NO_THINKING = {"thinking_budget": 0}


class AIProvider(Protocol):
    def complete(
        self,
        system_prompt: str,
        user_message: str,
        context: dict,
        schema: dict | None = None,
    ) -> str:
        """Return a JSON string matching AIAction.

        ``schema`` lets a provider constrain decoding server-side. Providers
        that cannot do that ignore it; the response is validated either way.
        """

    def explain(self, system_prompt: str, user_message: str, context: dict) -> str:
        """Return free-text analysis of already collected diagnostics."""


def _build_prompt(user_message: str, context: dict) -> str:
    return (
        f"Network context:\n{json.dumps(context, indent=2)}\n\n"
        f"Request: {user_message}"
    )


class GeminiProvider:
    """Google Gemini, via the google-genai SDK."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", client_factory=None):
        if not api_key:
            raise ValueError("AI_API_KEY is required to use the Gemini provider.")
        self.api_key = api_key
        self.model = model
        self._client_factory = client_factory
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client

        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise AIProviderNotConfiguredError(
                "The 'google-genai' package is not installed. "
                "Run: pip install google-genai"
            ) from exc

        self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _generate(
        self,
        system_prompt: str,
        user_message: str,
        context: dict,
        json_mode: bool,
        schema: dict | None = None,
    ) -> str:
        config = {
            "system_instruction": system_prompt,
            "max_output_tokens": DEFAULT_MAX_TOKENS,
            "temperature": DEFAULT_TEMPERATURE,
        }
        if json_mode:
            # Ask the API itself to guarantee JSON, so AIAction parsing does not
            # depend on the model remembering to skip prose or code fences.
            config["response_mime_type"] = "application/json"
            config["thinking_config"] = dict(NO_THINKING)
            if schema is not None:
                config["response_schema"] = schema

        contents = _build_prompt(user_message, context)
        try:
            response = self._call(contents, config)
        except AIProviderNotConfiguredError:
            raise
        except Exception as exc:
            # The upstream message can echo the request, including the API key,
            # so only the exception type is surfaced.
            logger.exception("Gemini request failed.")
            raise AIProviderError(
                f"The Gemini API call failed ({type(exc).__name__})."
            ) from exc

        return response.text or ""

    def _call(self, contents: str, config: dict):
        client = self._get_client()
        try:
            return client.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception:
            if "thinking_config" not in config:
                raise
            # Some models (gemini-2.5-pro among them) refuse to disable
            # thinking. Retry once without it rather than failing the request.
            logger.warning(
                "%s rejected thinking_config; retrying without it.", self.model
            )
            retry = {k: v for k, v in config.items() if k != "thinking_config"}
            return client.models.generate_content(
                model=self.model, contents=contents, config=retry
            )

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        context: dict,
        schema: dict | None = None,
    ) -> str:
        return self._generate(
            system_prompt, user_message, context, json_mode=True, schema=schema
        )

    def explain(self, system_prompt: str, user_message: str, context: dict) -> str:
        return self._generate(system_prompt, user_message, context, json_mode=False)


class AnthropicProvider:
    """Claude-backed provider. Requires the `anthropic` package and an API key."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-5", client_factory=None):
        if not api_key:
            raise ValueError("AI_API_KEY is required to use the Anthropic provider.")
        self.api_key = api_key
        self.model = model
        self._client_factory = client_factory
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client

        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise AIProviderNotConfiguredError(
                "The 'anthropic' package is not installed. "
                "Run: pip install anthropic, or set AI_PROVIDER=gemini."
            ) from exc

        self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def _message(self, system_prompt: str, user_message: str, context: dict) -> str:
        try:
            response = self._get_client().messages.create(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=DEFAULT_TEMPERATURE,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": _build_prompt(user_message, context)}
                ],
            )
        except AIProviderNotConfiguredError:
            raise
        except Exception as exc:
            logger.exception("Anthropic request failed.")
            raise AIProviderError(
                f"The Anthropic API call failed ({type(exc).__name__})."
            ) from exc

        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        )

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        context: dict,
        schema: dict | None = None,
    ) -> str:
        # Claude has no server-side schema enforcement here; the schema is
        # already described in the system prompt and validated on the way back.
        return self._message(system_prompt, user_message, context)

    def explain(self, system_prompt: str, user_message: str, context: dict) -> str:
        return self._message(system_prompt, user_message, context)


class NullProvider:
    """Used when no API key is configured, so the app still boots."""

    MESSAGE = (
        "No AI provider is configured. Set AI_API_KEY (and AI_PROVIDER) to enable "
        "/api/ai/chat. Every other endpoint works without it."
    )

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        context: dict,
        schema: dict | None = None,
    ) -> str:
        raise AIProviderNotConfiguredError(self.MESSAGE)

    def explain(self, system_prompt: str, user_message: str, context: dict) -> str:
        raise AIProviderNotConfiguredError(self.MESSAGE)


GEMINI_NAMES = {"gemini", "google", "google-genai"}
ANTHROPIC_NAMES = {"anthropic", "claude"}


def build_provider():
    """Resolve the provider from app config, honouring a test override."""
    from flask import current_app

    override = current_app.config.get("AI_PROVIDER_INSTANCE")
    if override is not None:
        return override

    name = (current_app.config.get("AI_PROVIDER") or "").strip().lower()
    api_key = current_app.config.get("AI_API_KEY")
    model = current_app.config.get("AI_MODEL")

    if api_key:
        if name in GEMINI_NAMES:
            return GeminiProvider(api_key, model or "gemini-2.5-flash")
        if name in ANTHROPIC_NAMES:
            return AnthropicProvider(api_key, model or "claude-sonnet-5")
        logger.warning("Unknown AI_PROVIDER %r; falling back to NullProvider.", name)
    else:
        logger.warning("AI_API_KEY is not set; falling back to NullProvider.")

    return NullProvider()
