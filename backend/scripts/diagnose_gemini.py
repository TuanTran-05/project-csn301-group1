"""Find which part of the copilot's Gemini request the API rejects.

Sends "hi" with the app's generation config built up one option at a time
and prints the API's verdict for each step. Reads AI_API_KEY and AI_MODEL
from backend/.env or the environment. The key is never printed.

    python scripts/diagnose_gemini.py
"""

import os
import sys
from pathlib import Path

from google import genai

from dotenv import load_dotenv
from network_copilot.ai.design_assistant import RESPONSE_SCHEMA as DESIGN_SCHEMA
from network_copilot.ai.provider import DEFAULT_MAX_TOKENS, NO_THINKING
from network_copilot.ai.schemas import build_ai_action_schema


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    api_key = os.environ.get("AI_API_KEY")
    model = os.environ.get("AI_MODEL", "gemini-2.5-flash")
    if not api_key:
        print("AI_API_KEY is not set in backend/.env.")
        return 1

    base = {"system_instruction": "Reply briefly."}
    json_mode = {**base, "response_mime_type": "application/json"}
    steps = [
        ("plain", base),
        ("+max_output_tokens", {**base, "max_output_tokens": DEFAULT_MAX_TOKENS}),
        ("+temperature=0", {**base, "temperature": 0}),
        ("+json mime", json_mode),
        ("+thinking_budget=0", {**json_mode, "thinking_config": dict(NO_THINKING)}),
        ("+chat schema", {**json_mode, "response_json_schema": build_ai_action_schema(["R1", "SW1"])}),
        ("+design schema", {**json_mode, "response_json_schema": DESIGN_SCHEMA}),
    ]

    client = genai.Client(api_key=api_key)
    print(f"model={model}")
    for name, config in steps:
        try:
            client.models.generate_content(model=model, contents="hi", config=config)
            print(f"OK    {name}")
        except Exception as exc:
            code = getattr(exc, "code", "")
            message = (getattr(exc, "message", "") or str(exc)).replace(api_key, "<key>")
            print(f"FAIL  {name}: {code} {message[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
