"""Gemini client.

An unavailable model is an error, not a degraded mode: a triage reply that silently
omits half its analysis renders identically to a complete one, and nobody re-reads it
during an incident to check.
"""

import json
import logging
import time
from typing import Any

from google import genai
from google.genai import types

from .config import Settings

# The SDK warns about automatic function calling on every call. No tools are declared
# here, so the warning is pure noise in the logs.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


class LLMUnavailable(RuntimeError):
    """Raised when the model cannot be reached or is not configured."""


# Overload is transient and common at peak. Giving up on the first 503 would make the
# agent unavailable exactly when incidents cluster.
_TRANSIENT = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL")


def _is_transient(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in _TRANSIENT)


class LLMClient:
    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise LLMUnavailable(
                "GEMINI_API_KEY is not set. The agent needs a model to analyze alerts; "
                "it will not emit a partial answer instead."
            )
        self.settings = settings
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def _call(self, model: str, prompt: str, config):
        """Call the model, retrying transient failures with a growing delay.

        Bounded deliberately: this runs during an incident, where a late answer is worth
        little. Better to fail clearly at ~20s than to keep an engineer waiting.
        """
        last: Exception | None = None
        for attempt in range(self.settings.llm_max_attempts):
            try:
                return self._client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
            except Exception as exc:
                last = exc
                if not _is_transient(exc) or attempt == self.settings.llm_max_attempts - 1:
                    break
                time.sleep(2 ** attempt)

        if _is_transient(last):
            raise LLMUnavailable(
                f"{model} is overloaded and did not recover after "
                f"{self.settings.llm_max_attempts} attempts. Try again shortly."
            ) from last
        raise LLMUnavailable(f"Gemini call failed ({model}): {last}") from last

    def generate(self, prompt: str, *, deep: bool = False, system: str | None = None) -> str:
        model = self.settings.gemini_model_deep if deep else self.settings.gemini_model_fast
        config = types.GenerateContentConfig(system_instruction=system) if system else None
        response = self._call(model, prompt, config)

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
        response = self._call(model, prompt, config)

        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMUnavailable(f"Gemini returned unparseable JSON: {exc}") from exc
