"""Slack listeners: ack in under three seconds, then work off the socket.

The 3-second deadline applies to Socket Mode exactly as it does to the Events API, so
`ack()` is the first statement in every listener and nothing that touches Grafana, Milvus or
an LLM runs before it (spec §8.1). The turn itself is an `asyncio.Task`.

That task catches everything. Failing more than 95% of deliveries in an hour disables the
app's event subscriptions and needs a manual re-enable (spec §4), so an exception here is
posted into the thread rather than allowed to escape and crash-loop the process — a bot that
says "that failed" is recoverable, a bot Slack has switched off is not.

No banner strings are built here. Spec §9 item 19: there is exactly one renderer, and this
adapter's only contribution is the markup argument and the mrkdwn rewrite.
"""

import asyncio
import re
from typing import Any

from loguru import logger

from app.config import Settings
from app.graph.build import run_turn
from app.render.answer import render_answer
from app.slack.dedupe import Dedupe
from app.slack.mrkdwn import to_mrkdwn
from app.slack.progress import ProgressWriter
from app.slack.router import TURNS, TurnLog, decide
from app.slack.thread import (
    BotIdentity,
    alert_message,
    clean_text,
    fetch_thread,
    priors,
    thread_digest,
)

ONCALL_ACTION = re.compile(r"^oncall_")
BUSY_NOTICE = "I'm still working on the previous question in this thread — one moment."
_TASKS: set[asyncio.Task] = set()


class SlackRuntime:
    """Everything a turn needs that is not the event itself."""

    def __init__(
        self,
        settings: Settings,
        identity: BotIdentity,
        dedupe: Dedupe,
        *,
        turns: TurnLog | None = None,
        checkpointer: Any = None,
    ) -> None:
        self.settings = settings
        self.identity = identity
        self.dedupe = dedupe
        self.turns = turns or TURNS
        self.checkpointer = checkpointer
        self.busy: set[str] = set()

    def thread_id(self, team: str, channel: str, thread_ts: str) -> str:
        return f"slack:{team}:{channel}:{thread_ts}"


def _spawn(coro: Any) -> asyncio.Task:
    """Keep a reference: a task the event loop is the only holder of can be collected."""
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def _say(client: Any, channel: str, thread_ts: str, text: str) -> None:
    try:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    except Exception as exc:
        logger.error(f"could not post into {channel}/{thread_ts}: {exc}")


async def _final_state(runtime: SlackRuntime, thread_id: str) -> dict[str, Any] | None:
    """The finished state, when a checkpointer kept one. Without it there is nothing to read."""
    if runtime.checkpointer is None:
        return None
    try:
        from app.graph.build import graph_for

        graph = graph_for(runtime.settings, runtime.checkpointer)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values) if snapshot and snapshot.values else None
    except Exception as exc:
        logger.warning(f"could not read final state for {thread_id}: {exc}")
        return None


async def run_slack_turn(
    event: dict[str, Any],
    body: dict[str, Any],
    client: Any,
    runtime: SlackRuntime,
) -> str:
    """One turn, from thread fetch to final message. Returns what was posted."""
    settings = runtime.settings
    channel = event.get("channel") or ""
    thread_ts = event.get("thread_ts") or event.get("ts") or ""
    team = body.get("team_id") or event.get("team") or runtime.identity.team_id or "unknown"
    thread_id = runtime.thread_id(team, channel, thread_ts)
    event_id = body.get("event_id") or body.get("trigger_id") or event.get("client_msg_id") or ""

    if not await runtime.dedupe.claim(event_id):
        logger.info(f"event {event_id} already claimed; another turn owns it")
        return ""

    if thread_id in runtime.busy:
        await _say(client, channel, thread_ts, BUSY_NOTICE)
        return BUSY_NOTICE
    runtime.busy.add(thread_id)

    try:
        try:
            messages = await fetch_thread(
                client,
                channel,
                thread_ts,
                settings.slack_thread_limit,
                identity=runtime.identity,
            )
        except Exception as exc:
            logger.warning(f"thread fetch failed for {thread_id}: {exc}")
            messages = []

        decision = await decide(
            event, messages, settings=settings, prior_turns=runtime.turns.turns(thread_id)
        )
        logger.info(f"{thread_id} → {decision.turn} (rule {decision.rule}: {decision.because})")

        question = clean_text(event.get("text"))
        alert = alert_message(messages)
        state_in: dict[str, Any] = {
            "input": question,
            "turn": decision.turn,
            "conversation_id": thread_id,
            "alert_text": alert.text if alert else question,
            "priors": priors(messages),
            "thread_digest": thread_digest(messages),
        }

        progress = ProgressWriter(
            client,
            channel,
            thread_ts,
            interval=settings.slack_progress_interval,
            use_streaming=settings.slack_use_streaming,
        )

        lines: list[str] = []
        answer, failure = "", ""
        async for event_out in run_turn(
            state_in,
            settings=settings,
            thread_id=thread_id,
            checkpointer=runtime.checkpointer,
        ):
            kind = event_out.get("type")
            if kind == "error":
                failure = event_out.get("message") or "unknown failure"
            if event_out.get("response"):
                answer = event_out["response"]
            if kind != "complete" and event_out.get("message"):
                lines.append(str(event_out["message"]))
                await progress.update(lines)

        state = await _final_state(runtime, thread_id)
        if state:
            answer = render_answer(state, markup="slack")  # type: ignore[arg-type]
        if not answer:
            reason = failure or "no answer was produced"
            answer = f":warning: I could not finish this one — {reason}"

        if decision.offer_triage:
            # A chat answer inside an alert thread is a routing judgement, and the engineer
            # is the one who can overrule it in a word. Saying nothing would make the
            # misroute invisible, which is the only version of it that is expensive.
            answer += (
                "\n\n_I read this as a question rather than a request to diagnose the "
                "alert in this thread, so I haven't investigated it. Say *investigate* "
                "and I will._"
            )

        text = to_mrkdwn(answer)
        await progress.finish(text)
        runtime.turns.record(thread_id, decision.turn)
        return text
    except Exception:
        await runtime.dedupe.release(event_id)
        raise
    finally:
        runtime.busy.discard(thread_id)


async def _guarded(
    event: dict[str, Any], body: dict[str, Any], client: Any, runtime: SlackRuntime
) -> None:
    try:
        await run_slack_turn(event, body, client, runtime)
    except Exception as exc:
        logger.exception(f"slack turn failed: {exc}")
        await _say(
            client,
            event.get("channel") or "",
            event.get("thread_ts") or event.get("ts") or "",
            f":warning: That turn failed — {type(exc).__name__}: {exc}",
        )


def _event_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """An interaction payload, shaped like a message event so the router reads one thing."""
    message = body.get("message") or {}
    container = body.get("container") or {}
    channel = (body.get("channel") or {}).get("id") or body.get("channel_id") or ""
    thread_ts = (
        message.get("thread_ts")
        or container.get("thread_ts")
        or container.get("message_ts")
        or message.get("ts")
        or body.get("message_ts")
        or ""
    )
    return {
        "channel": channel,
        "ts": thread_ts,
        "thread_ts": thread_ts,
        "user": (body.get("user") or {}).get("id") or body.get("user_id") or "",
        "text": clean_text(body.get("text") or ""),
        "actions": body.get("actions") or [],
        "command": body.get("command") or "",
        "callback_id": body.get("callback_id") or "",
    }


def register(app: Any, runtime: SlackRuntime) -> None:
    """Wire the listeners. Every one of them acks first and works in a task."""

    @app.event("app_mention")
    async def on_mention(event, body, ack, client):  # type: ignore[no-untyped-def]
        await ack()
        _spawn(_guarded(dict(event), dict(body), client, runtime))

    @app.event("message")
    async def on_message(event, body, ack, client):  # type: ignore[no-untyped-def]
        await ack()
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        if runtime.identity.wrote(user=event.get("user")):
            return
        _spawn(_guarded(dict(event), dict(body), client, runtime))

    @app.action(ONCALL_ACTION)
    async def on_action(body, ack, client):  # type: ignore[no-untyped-def]
        await ack()
        _spawn(_guarded(_event_from_body(dict(body)), dict(body), client, runtime))

    @app.command("/oncall")
    async def on_command(body, ack, client):  # type: ignore[no-untyped-def]
        await ack()
        _spawn(_guarded(_event_from_body(dict(body)), dict(body), client, runtime))
