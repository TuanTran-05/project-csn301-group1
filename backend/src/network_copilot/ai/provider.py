"""AI provider adapters.

A provider only turns text into text. It never receives credentials, never
opens an SSH session and never decides whether something may run.
"""

import json
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class AIProvider(Protocol):
    def complete(self, system_prompt: str, user_message: str, context: dict) -> str:
        """Return a JSON string matching AIAction."""

    def explain(self, system_prompt: str, user_message: str, context: dict) -> str:
        """Return free-text analysis of already collected diagnostics."""


class AnthropicProvider:
    """Claude-backed provider. Requires the `anthropic` package and an API key."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-5"):
        if not api_key:
            raise ValueError("AI_API_KEY is required to use the Anthropic provider.")
        self.api_key = api_key
        self.model = model

    def _client(self):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "The 'anthropic' package is not installed. "
                "Install it or configure a different AI_PROVIDER."
            ) from exc
        return anthropic.Anthropic(api_key=self.api_key)

    def _message(self, system_prompt: str, user_message: str, context: dict) -> str:
        response = self._client().messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Network context:\n{json.dumps(context, indent=2)}\n\n"
                        f"Request: {user_message}"
                    ),
                }
            ],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

    def complete(self, system_prompt: str, user_message: str, context: dict) -> str:
        return self._message(system_prompt, user_message, context)

    def explain(self, system_prompt: str, user_message: str, context: dict) -> str:
        return self._message(system_prompt, user_message, context)


class NullProvider:
    """Used when no API key is configured, so the app still boots."""

    def complete(self, system_prompt: str, user_message: str, context: dict) -> str:
        raise RuntimeError(
            "No AI provider is configured. Set AI_API_KEY to enable /api/ai/chat."
        )

    def explain(self, system_prompt: str, user_message: str, context: dict) -> str:
        raise RuntimeError(
            "No AI provider is configured. Set AI_API_KEY to enable /api/ai/chat."
        )


def build_provider():
    """Resolve the provider from app config, honouring a test override."""
    from flask import current_app

    override = current_app.config.get("AI_PROVIDER_INSTANCE")
    if override is not None:
        return override

    name = (current_app.config.get("AI_PROVIDER") or "").lower()
    api_key = current_app.config.get("AI_API_KEY")

    if name == "anthropic" and api_key:
        return AnthropicProvider(api_key, current_app.config.get("AI_MODEL"))

    logger.warning("No usable AI provider configured; falling back to NullProvider.")
    return NullProvider()
