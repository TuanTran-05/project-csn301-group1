"""Deterministic stand-in for a real LLM provider.

Records every prompt it receives so tests can assert that no credential, token
or full running-config was ever sent to the model.
"""

import json


class FakeAIProvider:
    def __init__(self, responses=None, raise_with: Exception | None = None):
        # Either a single dict/AIAction payload or a list consumed in order.
        self.responses = responses
        self.raise_with = raise_with
        self.prompts: list[dict] = []
        self.calls = 0

    def complete(self, system_prompt: str, user_message: str, context: dict) -> str:
        self.prompts.append(
            {
                "system_prompt": system_prompt,
                "user_message": user_message,
                "context": context,
            }
        )
        self.calls += 1

        if self.raise_with is not None:
            raise self.raise_with

        payload = self.responses
        if isinstance(payload, list):
            payload = payload[min(self.calls - 1, len(payload) - 1)]
        if isinstance(payload, str):
            return payload
        return json.dumps(payload)

    def explain(self, system_prompt: str, user_message: str, context: dict) -> str:
        """Free-text phase used for troubleshooting explanations."""
        self.prompts.append(
            {
                "system_prompt": system_prompt,
                "user_message": user_message,
                "context": context,
                "mode": "explain",
            }
        )
        self.calls += 1
        if self.raise_with is not None:
            raise self.raise_with
        return "OSPF adjacency is up on GigabitEthernet0/2."

    # -- assertions helpers ------------------------------------------------
    def everything_sent(self) -> str:
        return json.dumps(self.prompts)
