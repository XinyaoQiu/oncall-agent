"""`POST /triage` — one turn of the graph, streamed as SSE.

The events are `run_turn`'s, unmodified: Slack's progress writer and this endpoint read the
same stream (spec §8.3), because two event streams drift and the one that drifts is the one
that silently stops disclosing. The envelope — `{"event": "message", "data": <json>}`,
terminating on `complete` or `error` — is the shape the sibling project's web UI already
consumes, so a browser pointed at this endpoint needs no new client.

One rule is enforced *before* the stream opens. If no model is configured or reachable, the
reply is an `error` event and nothing else. The graph would otherwise run its floor, fail to
produce a diagnosis and render a pack whose analysis section is missing — and a pack missing
its analysis renders exactly like a complete one to anyone skimming it in an incident
(spec §9 constraint 14).
"""

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.config import Settings, get_settings
from app.core.llm_factory import LLMUnavailable, get_llm
from app.graph.build import run_turn
from app.storage.records import RecordStore

router = APIRouter()

TERMINAL = ("complete", "error")


class TriageRequest(BaseModel):
    input: str = Field(description="What the engineer is asking")
    conversation_id: str | None = Field(
        default=None, description="Reuse to continue a conversation; one is minted otherwise"
    )
    alert_text: str | None = Field(
        default=None, description="The raw alert, when it differs from the question"
    )


def _sse(event: dict[str, Any]) -> dict[str, str]:
    return {"event": "message", "data": json.dumps(event, ensure_ascii=False)}


def _error(message: str, stage: str = "error") -> dict[str, Any]:
    return {"type": "error", "stage": stage, "message": message}


async def _record(
    store: RecordStore | None,
    body: TriageRequest,
    conversation_id: str,
    events: list[dict[str, Any]],
    started: float,
) -> None:
    """Record what the stream carried. Deliberately thinner than the CLI's row.

    The SSE adapter never holds the final state — it holds events — so the observations and
    the diagnosis are not available here to write down. A row that says so is worth having;
    one reconstructed from event text would be a fabrication in the table the evaluation
    reads.
    """
    if store is None:
        return
    answers = [e for e in events if e["type"] in ("answer", "complete") and e.get("response")]
    failures = [e for e in events if e["type"] == "error"]
    baseline = next((e for e in events if e["type"] == "evidence"), {})

    await store.record(
        {
            "alert_name": baseline.get("alert"),
            "turn": "triage",
            "conversation_id": conversation_id,
            "response": answers[-1]["response"] if answers else "",
        },
        source="api",
        conversation_id=conversation_id,
        question=body.input,
        duration_ms=int((time.time() - started) * 1000),
        rounds=len([e for e in events if e["type"] == "step"]),
        error=failures[-1]["message"] if failures else None,
    )


async def stream_turn(
    body: TriageRequest, *, settings: Settings, store: RecordStore | None
) -> AsyncIterator[dict[str, str]]:
    conversation_id = body.conversation_id or f"api:{uuid.uuid4()}"
    started = time.time()

    try:
        get_llm(settings)
    except LLMUnavailable as exc:
        logger.error(f"triage refused, no model: {exc}")
        yield _sse(_error(f"{exc}", stage="model"))
        return

    state_in: dict[str, Any] = {
        "input": body.input,
        "turn": "triage",
        "conversation_id": conversation_id,
        "alert_text": body.alert_text or body.input,
    }

    seen: list[dict[str, Any]] = []
    try:
        async for event in run_turn(
            state_in, settings=settings, thread_id=conversation_id, checkpointer=None
        ):
            seen.append(event)
            yield _sse(event)
            if event["type"] in TERMINAL:
                break
    except Exception as exc:
        logger.exception(f"triage stream for {conversation_id} failed")
        failure = _error(f"{type(exc).__name__}: {exc}", stage="exception")
        seen.append(failure)
        yield _sse(failure)

    await _record(store, body, conversation_id, seen, started)


@router.post("/triage")
async def triage(request: Request, body: TriageRequest) -> EventSourceResponse:
    """Run one turn and stream it. See the module docstring for the event contract."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    store = getattr(request.app.state, "store", None)
    logger.info(f"triage request: {body.input[:120]!r}")
    return EventSourceResponse(stream_turn(body, settings=settings, store=store))
