"""Where a chat model comes from, and what happens when there isn't one.

An unavailable model is an **error**, not a degraded mode (tech-design §8.1). The failure
this guards against is specific: a triage reply that quietly lost its analysis section
renders identically to a complete one, and nobody re-reads it during an incident to check.
So there is no "return a stub and carry on" path here — construction either yields a model
or raises.

Which tier answered still travels with the reply (`state["degraded_model"]`), because a
weaker model writes with exactly the same confidence as the strong one.
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from loguru import logger

from app.config import Settings

PROVIDERS = ("dashscope", "openai")


class LLMUnavailable(RuntimeError):
    """The model cannot be reached or is not configured."""


def model_name(settings: Settings, *, deep: bool = False) -> str:
    return settings.model_deep if deep else settings.model_fast


def _dashscope(settings: Settings, model: str, **kwargs: Any) -> BaseChatModel:
    if not settings.dashscope_api_key:
        raise LLMUnavailable(
            "DASHSCOPE_API_KEY is not set. The agent needs a model to analyze alerts; "
            "it will not emit a partial answer instead."
        )
    try:
        from langchain_qwq import ChatQwen
    except ImportError as exc:
        raise LLMUnavailable(
            f"llm_provider is 'dashscope' but langchain-qwq is missing: {exc}"
        ) from exc

    options: dict[str, Any] = {"model": model, "api_key": settings.dashscope_api_key}
    if settings.dashscope_api_base:
        options["api_base"] = settings.dashscope_api_base
    return ChatQwen(temperature=0, **options, **kwargs)


def _openai(settings: Settings, model: str, **kwargs: Any) -> BaseChatModel:
    if not settings.openai_api_key:
        raise LLMUnavailable(
            "OPENAI_API_KEY is not set. The agent needs a model to analyze alerts; "
            "it will not emit a partial answer instead."
        )
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise LLMUnavailable(
            f"llm_provider is 'openai' but langchain-openai is missing: {exc}"
        ) from exc

    options: dict[str, Any] = {"model": model, "api_key": settings.openai_api_key}
    if settings.openai_api_base:
        options["base_url"] = settings.openai_api_base
    return ChatOpenAI(temperature=0, **options, **kwargs)


def get_llm(settings: Settings, *, deep: bool = False, **kwargs: Any) -> BaseChatModel:
    """The chat model for this tier, or `LLMUnavailable`.

    `deep` selects the stronger, slower tier — used for the diagnosis, where the judgment
    is the product. Planning and re-planning run on the fast tier.
    """
    provider = (settings.llm_provider or "").strip().lower()
    model = model_name(settings, deep=deep)

    if provider == "dashscope":
        built = _dashscope(settings, model, **kwargs)
    elif provider == "openai":
        built = _openai(settings, model, **kwargs)
    else:
        raise LLMUnavailable(
            f"unknown llm_provider {settings.llm_provider!r}; expected one of {PROVIDERS}"
        )

    logger.debug(f"llm: {provider}/{model} (deep={deep})")
    return built
