"""Gemini client.

An unavailable model is an error, not a degraded mode: a triage reply that silently
omits half its analysis renders identically to a complete one, and nobody re-reads it
during an incident to check.
"""

import json
from typing import Any

from google import genai
from google.genai import types

from .config import Settings


class LLMUnavailable(RuntimeError):
    """Raised when the model cannot be reached or is not configured."""


class LLMClient:
    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise LLMUnavailable(
                "GEMINI_API_KEY is not set. The agent needs a model to analyze alerts; "
                "it will not emit a partial answer instead."
            )
        self.settings = settings
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate(self, prompt: str, *, deep: bool = False, system: str | None = None) -> str:
        model = self.settings.gemini_model_deep if deep else self.settings.gemini_model_fast
        config = types.GenerateContentConfig(system_instruction=system) if system else None
        try:
            response = self._client.models.generate_content(
                model=model, contents=prompt, config=config
            )
        except Exception as exc:
            raise LLMUnavailable(f"Gemini call failed ({model}): {exc}") from exc

        if not response.text:
            raise LLMUnavailable(f"Gemini returned an empty response ({model})")
        return response.text

    def generate_json(
        self, prompt: str, schema: dict[str, Any], *, deep: bool = False, system: str | None = None
    ) -> dict[str, Any]:
        """Structured output. The schema is the constraint, not a prompt instruction."""
        model = self.settings.gemini_model_deep if deep else self.settings.gemini_model_fast
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=system,
        )
        try:
            response = self._client.models.generate_content(
                model=model, contents=prompt, config=config
            )
        except Exception as exc:
            raise LLMUnavailable(f"Gemini call failed ({model}): {exc}") from exc

        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMUnavailable(f"Gemini returned unparseable JSON: {exc}") from exc
