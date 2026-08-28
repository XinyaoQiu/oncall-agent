"""Is this mention asking me to investigate the alert, or asking me something else?

Structural routing (router.py) answers a question Slack already knows the answer to: does
this thread hang off an alert. That one is a fact and belongs in code. This one is not —
"is it the cold start thing again?" and "what is server-feed?" are both questions typed into
the same alert thread, and only their meaning separates them.

Two things keep a semantic gate from becoming the failure spec §2.2 exists to prevent.

The misroutes are not symmetric. Answering a definitional question with a full
investigation is slow and noisy, but nothing is lost. Answering an investigation request
with a definition means the engineer believes triage happened when it did not — silent
under-delivery, during an incident. So uncertainty resolves to INVESTIGATE, in code, not as
a suggestion in the prompt.

And a chat answer inside an alert thread is disclosed, with a one-click upgrade. A misroute
the engineer can see and correct in one word is a nuisance; a silent one is the thing worth
designing against.
"""

from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field

from app.config import Settings

Intent = Literal["investigate", "ask"]

SYSTEM_PROMPT = """You classify one message from an engineer in a Slack thread that hangs
off a production alert.

Return "investigate" when they want the alert looked into — including when they phrase it
as a narrow hypothesis ("is it the cold start thing again?", "did something deploy?",
"CPU looks fine to me"). A narrow framing is still a request to diagnose THIS alert, and
narrowing what gets collected because of it is a known way to miss the cause.

Return "ask" only when the message is not about diagnosing this alert at all — a
definitional or reference question that happens to be typed in this thread. Examples:
"what is server-feed", "what does error code 43 mean", "who owns this service",
"where is that config".

If you are not sure, return "investigate"."""


class IntentVerdict(BaseModel):
    intent: Intent = Field(description="investigate | ask")
    reason: str = ""


async def classify_intent(text: str, *, settings: Settings) -> tuple[Intent, str]:
    """Classify, biased toward investigating. Never raises."""
    stripped = (text or "").strip()

    # A bare mention in an alert thread is the summons itself (spec §3.1).
    if not stripped:
        return "investigate", "bare mention in an alert thread"

    try:
        from app.core.llm_factory import llm_factory

        llm = llm_factory.create_chat_model(
            model=settings.rag_model, temperature=0, streaming=False
        )
        verdict = await llm.with_structured_output(IntentVerdict).ainvoke(
            [("system", SYSTEM_PROMPT), ("user", stripped[:2000])]
        )
        intent = getattr(verdict, "intent", None) or "investigate"
        reason = getattr(verdict, "reason", "") or ""
        if intent not in ("investigate", "ask"):
            return "investigate", f"unrecognised intent {intent!r}; defaulted to investigate"
        return intent, reason
    except Exception as exc:
        # The safe direction is the expensive one: over-investigating wastes a minute,
        # under-investigating loses the incident.
        logger.warning(f"intent classification failed, defaulting to investigate: {exc}")
        return "investigate", "classifier unavailable; defaulted to investigate"
