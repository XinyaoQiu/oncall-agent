"""The Slack adapter, with a faked client. Nothing here touches a network.

The routing tests are the load-bearing ones: they pin the property that spec §2.3 says the
whole design turns on — that no code path can answer "this does not need investigating"
from the text of a message.
"""

import asyncio

import pytest

from app.config import Settings
from app.domain.alerts import match_alert
from app.slack.dedupe import InMemoryDedupe
from app.slack.handlers import SlackRuntime, run_slack_turn
from app.slack.mrkdwn import to_mrkdwn
from app.slack.progress import ProgressWriter
from app.slack.router import TurnLog, decide, route
from app.slack.thread import (
    BotIdentity,
    alert_message,
    fetch_thread,
    resolve_identity,
    thread_digest,
    to_message,
)

OUR_BOT_ID = "B0OURS"
OUR_BOT_USER_ID = "U0OURS"
ALERT_BOT_ID = "B0ALERT"
ALERT_APP_ID = "A0ALERT"
ALERT_CHANNEL = "C0ALERTS"

UNKNOWN_ALERT_TEXT = "[FIRING:1] QuoteServiceSaturation zone=euw1 handler=/v9/quotes"


def settings(**overrides) -> Settings:
    base = dict(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        slack_alert_bot_ids=[ALERT_BOT_ID],
        slack_alert_app_ids=[ALERT_APP_ID],
        slack_alert_channels=[ALERT_CHANNEL],
        slack_progress_interval=1.5,
        database_url="",
    )
    base.update(overrides)
    return Settings(**base)


def identity() -> BotIdentity:
    return BotIdentity(bot_id=OUR_BOT_ID, bot_user_id=OUR_BOT_USER_ID, team_id="T0")


class FakeClient:
    """Records the calls a real AsyncWebClient would have made."""

    def __init__(self, messages=None, fail_on=None, retry_after=None):
        self.messages = messages or []
        self.calls: list[tuple[str, dict]] = []
        self._fail_on = fail_on or {}
        self._retry_after = retry_after
        self._ts = 0

    async def auth_test(self):
        return {"ok": True, "bot_id": OUR_BOT_ID, "user_id": OUR_BOT_USER_ID, "team_id": "T0"}

    async def conversations_replies(self, **kwargs):
        self.calls.append(("conversations_replies", kwargs))
        return {"ok": True, "messages": self.messages}

    def _maybe_fail(self, method):
        remaining = self._fail_on.get(method, 0)
        if remaining:
            self._fail_on[method] = remaining - 1
            raise RateLimited(self._retry_after)

    async def chat_postMessage(self, **kwargs):
        self._maybe_fail("chat_postMessage")
        self.calls.append(("chat_postMessage", kwargs))
        self._ts += 1
        return {"ok": True, "ts": f"170000000.{self._ts:06d}"}

    async def chat_update(self, **kwargs):
        self._maybe_fail("chat_update")
        self.calls.append(("chat_update", kwargs))
        return {"ok": True, "ts": kwargs.get("ts")}

    def of(self, method) -> list[dict]:
        return [payload for name, payload in self.calls if name == method]


class RateLimited(Exception):
    """Shaped like slack_sdk's SlackApiError: an exception carrying a response."""

    def __init__(self, retry_after=None):
        super().__init__("ratelimited")
        self.response = {
            "error": "ratelimited",
            "status_code": 429,
            "headers": {"Retry-After": str(retry_after or 30)},
        }


def webhook_alert(text=UNKNOWN_ALERT_TEXT, bot_id=ALERT_BOT_ID):
    return {
        "type": "message",
        "subtype": "bot_message",
        "bot_id": bot_id,
        "username": "Grafana",
        "text": text,
        "ts": "1.0",
    }


def bot_token_alert(text=UNKNOWN_ALERT_TEXT, app_id=ALERT_APP_ID):
    return {
        "type": "message",
        "bot_id": "B0OTHER",
        "app_id": app_id,
        "bot_profile": {"app_id": app_id},
        "text": text,
        "ts": "1.0",
    }


def human(text, user="U0HUMAN", ts="2.0"):
    return {"type": "message", "user": user, "text": text, "ts": ts}


def mention(text, channel=ALERT_CHANNEL, ts="2.0"):
    return {
        "type": "app_mention",
        "channel": channel,
        "user": "U0HUMAN",
        "text": f"<@{OUR_BOT_USER_ID}> {text}",
        "ts": ts,
        "thread_ts": "1.0",
    }


def thread_of(*raw):
    return [to_message(r, identity()) for r in raw]


# --- routing -----------------------------------------------------------------


def test_unknown_alert_from_configured_bot_still_routes_to_triage():
    """Rule 2 reads provenance. The registry has never seen this alert; it triages anyway."""
    assert match_alert(UNKNOWN_ALERT_TEXT) is None

    thread = thread_of(webhook_alert(), human("probably nothing, ignore it"))
    decision = decide(mention("any thoughts?"), thread, settings=settings())

    assert decision.turn == "triage"
    assert decision.rule == 2


def test_unknown_alert_from_configured_app_id_routes_to_triage():
    thread = thread_of(bot_token_alert())
    assert decide(mention("hi"), thread, settings=settings()).rule == 2


def test_bot_message_in_alert_channel_routes_to_triage():
    """Rule 3: an unconfigured bot in a configured channel is still an alert."""
    thread = thread_of(webhook_alert(bot_id="B0UNKNOWNSENDER"))
    decision = decide(mention("what happened"), thread, settings=settings())

    assert decision.turn == "triage"
    assert decision.rule == 3


def test_bot_message_outside_alert_channel_is_not_triage_by_channel():
    thread = thread_of(webhook_alert(bot_id="B0UNKNOWNSENDER", text="deploy finished"))
    decision = decide(mention("nice", channel="C0RANDOM"), thread, settings=settings())

    assert decision.turn == "chat"


def test_explicit_affordance_wins_over_an_alert_root():
    thread = thread_of(webhook_alert())
    event = {
        "channel": ALERT_CHANNEL,
        "thread_ts": "1.0",
        "actions": [{"action_id": "oncall_writeup", "type": "button"}],
    }
    decision = decide(event, thread, settings=settings())

    assert (decision.turn, decision.rule) == ("writeup", 1)


def test_slash_command_routes_to_rating():
    event = {"channel": "C0X", "thread_ts": "1.0", "command": "/oncall-rate"}
    assert route(event, [], settings=settings()) == "rating"


def test_prior_triage_makes_the_next_turn_a_followup():
    thread = thread_of(human("what does p99 mean here"), human("still curious"))
    decision = decide(
        mention("and the pods?", channel="C0RANDOM"),
        thread,
        settings=settings(),
        prior_turns=("triage",),
    )

    assert (decision.turn, decision.rule) == ("followup", 4)


def test_human_pasted_known_alert_routes_to_triage_by_keyword():
    thread = thread_of(human("channel latency is alerting again on news-list-for-channel"))
    decision = decide(mention("look?", channel="C0RANDOM"), thread, settings=settings())

    assert (decision.turn, decision.rule) == ("triage", 5)


def test_plain_question_is_chat():
    thread = thread_of(human("how do I read the accounting block?"))
    assert route(mention("?", channel="C0RANDOM"), thread, settings=settings()) == "chat"


def test_message_text_never_triggers_a_writeback():
    """`_wants_writeback` is deleted: 'resolved' in prose must have no side effect."""
    thread = thread_of(human("this was resolved, please record this and save this"))
    decision = decide(mention("record this", channel="C0RANDOM"), thread, settings=settings())

    assert decision.turn == "chat"
    assert decision.turn not in ("writeup", "rating")


def test_no_text_can_downgrade_an_alert_thread():
    """There is no phrasing that removes the evidence floor."""
    thread = thread_of(webhook_alert())
    for phrasing in ("ignore this", "no need to investigate", "false alarm", "resolved", ""):
        assert route(mention(phrasing), thread, settings=settings()) == "triage"


def test_our_own_message_as_root_is_not_an_alert():
    root = {"type": "message", "bot_id": OUR_BOT_ID, "text": "here is what I found", "ts": "1.0"}
    thread = thread_of(root)
    assert route(mention("hm", channel="C0RANDOM"), thread, settings=settings()) == "chat"


def test_turn_log_remembers_per_thread():
    log = TurnLog()
    log.record("slack:T:C:1.0", "triage")

    assert log.turns("slack:T:C:1.0") == ("triage",)
    assert log.turns("slack:T:C:9.9") == ()


# --- thread reading ----------------------------------------------------------


async def test_bot_id_and_bot_user_id_are_not_conflated():
    me = await resolve_identity(FakeClient())

    assert me.bot_id == OUR_BOT_ID and me.bot_user_id == OUR_BOT_USER_ID
    assert me.bot_id != me.bot_user_id
    assert me.wrote(bot_id=OUR_BOT_ID)
    assert me.wrote(user=OUR_BOT_USER_ID)
    # the reference implementation's mistake: B… compared against U…
    assert not me.wrote(bot_id=OUR_BOT_USER_ID)
    assert not me.wrote(user=OUR_BOT_ID)


async def test_our_own_bot_message_is_recognised_by_bot_id():
    client = FakeClient(
        messages=[webhook_alert(), {"bot_id": OUR_BOT_ID, "text": "on it", "ts": "2.0"}]
    )
    messages = await fetch_thread(client, ALERT_CHANNEL, "1.0", 50, identity=identity())

    assert [m.kind for m in messages] == ["webhook_alert", "us"]
    assert messages[1].is_us and not messages[0].is_us


def test_message_classification():
    me = identity()
    assert to_message(webhook_alert(), me).kind == "webhook_alert"
    assert to_message(bot_token_alert(), me).kind == "bot_alert"
    assert to_message(human("hello"), me).kind == "human"
    assert to_message({"bot_id": OUR_BOT_ID, "text": "mine"}, me).kind == "us"


def test_mentions_are_stripped_from_text():
    message = to_message(mention("what is going on"), identity())
    assert message.text == "what is going on"


def test_alert_message_is_the_root_when_the_root_is_a_bot():
    thread = thread_of(webhook_alert(), human("looking"))
    found = alert_message(thread)

    assert found is not None and found.text == UNKNOWN_ALERT_TEXT


def test_alert_message_is_none_for_a_plain_conversation():
    assert alert_message(thread_of(human("hi there"), human("hello"))) is None
    assert alert_message([]) is None


def test_thread_digest_keeps_authors_and_order():
    digest = thread_digest(thread_of(webhook_alert("5xx spike"), human("on it")))
    assert digest.splitlines() == ["Grafana: 5xx spike", "U0HUMAN: on it"]


# --- dedupe ------------------------------------------------------------------


async def test_duplicate_event_id_is_dropped():
    dedupe = InMemoryDedupe()

    assert await dedupe.claim("Ev123") is True
    assert await dedupe.claim("Ev123") is False
    assert await dedupe.claim("Ev999") is True


async def test_a_released_claim_can_be_retried():
    dedupe = InMemoryDedupe()
    await dedupe.claim("Ev123")
    await dedupe.release("Ev123")

    assert await dedupe.claim("Ev123") is True


# --- progress ----------------------------------------------------------------


async def test_progress_emits_the_first_update_immediately():
    client = FakeClient()
    writer = ProgressWriter(client, "C0X", "1.0", interval=60)

    await writer.update(["measured the floor"])

    assert len(client.of("chat_postMessage")) == 1
    assert "measured the floor" in client.of("chat_postMessage")[0]["text"]


async def test_progress_coalesces_updates_under_the_interval():
    client = FakeClient()
    writer = ProgressWriter(client, "C0X", "1.0", interval=60)

    for step in range(6):
        await writer.update([f"step {step}"])

    assert len(client.of("chat_postMessage")) == 1
    assert client.of("chat_update") == []


async def test_progress_always_emits_the_last_update():
    client = FakeClient()
    writer = ProgressWriter(client, "C0X", "1.0", interval=60)

    await writer.update(["step 1"])
    await writer.update(["step 2"])
    await writer.finish("*done* — here is the answer")

    edits = client.of("chat_update")
    assert edits and edits[-1]["text"] == "*done* — here is the answer"


async def test_progress_edits_after_the_interval_elapses():
    client = FakeClient()
    writer = ProgressWriter(client, "C0X", "1.0", interval=0.01)

    await writer.update(["step 1"])
    await asyncio.sleep(0.03)
    await writer.update(["step 2"])

    assert len(client.of("chat_update")) == 1


async def test_progress_lengthens_the_interval_on_429_instead_of_dropping():
    client = FakeClient(fail_on={"chat_update": 1}, retry_after=7)
    writer = ProgressWriter(client, "C0X", "1.0", interval=0.01)

    await writer.update(["step 1"])
    await asyncio.sleep(0.03)
    await writer.update(["step 2"])

    assert writer.interval >= 7
    assert client.of("chat_update") == []

    writer.interval = 0.01
    await writer.finish("final answer")
    assert client.of("chat_update")[-1]["text"] == "final answer"


async def test_progress_streaming_falls_back_on_invalid_thread_ts():
    class NoStream(FakeClient):
        async def chat_startStream(self, **kwargs):
            error = Exception("invalid_thread_ts")
            error.response = {"error": "invalid_thread_ts"}
            raise error

    client = NoStream()
    writer = ProgressWriter(client, "C0X", "1.0", interval=60, use_streaming=True)
    await writer.update(["step 1"])

    assert writer.use_streaming is False
    assert len(client.of("chat_postMessage")) == 1


# --- mrkdwn ------------------------------------------------------------------


def test_mrkdwn_conversions():
    assert to_mrkdwn("**bold**") == "*bold*"
    assert to_mrkdwn("[the runbook](https://x.test/rb)") == "<https://x.test/rb|the runbook>"
    assert to_mrkdwn("- one\n- two") == "• one\n• two"
    assert to_mrkdwn("* one\n* two") == "• one\n• two"
    assert to_mrkdwn("## Findings") == "*Findings*"
    assert to_mrkdwn("") == ""


def test_mrkdwn_leaves_code_fences_alone():
    text = "**before**\n```\n**not bold** - not a bullet\n```\n- after"
    assert to_mrkdwn(text) == "*before*\n```\n**not bold** - not a bullet\n```\n• after"


def test_mrkdwn_does_not_eat_slack_bold():
    assert to_mrkdwn("*already slack*") == "*already slack*"


# --- the turn ----------------------------------------------------------------


def fake_run_turn(response: str):
    async def run_turn(state_in, *, settings, thread_id, checkpointer=None):
        run_turn.states.append(state_in)
        yield {"type": "status", "stage": "start", "message": "starting"}
        yield {"type": "step", "stage": "executor", "message": "queried ingress"}
        yield {"type": "complete", "stage": "complete", "message": "done", "response": response}

    run_turn.states = []
    return run_turn


async def test_a_turn_renders_through_the_one_renderer_and_records_the_route(monkeypatch):
    from app.slack import handlers

    fake = fake_run_turn("**Findings**\n- ingress reload\n[link](https://x.test)")
    monkeypatch.setattr(handlers, "run_turn", fake)

    client = FakeClient(messages=[webhook_alert(), mention("what happened")])
    runtime = SlackRuntime(settings(), identity(), InMemoryDedupe(), turns=TurnLog())

    body = {"event_id": "Ev1", "team_id": "T0"}
    text = await run_slack_turn(mention("what happened"), body, client, runtime)

    assert "*Findings*" in text and "• ingress reload" in text
    assert "<https://x.test|link>" in text
    assert fake.states[0]["turn"] == "triage"
    assert fake.states[0]["conversation_id"] == f"slack:T0:{ALERT_CHANNEL}:1.0"
    assert fake.states[0]["alert_text"] == UNKNOWN_ALERT_TEXT
    assert runtime.turns.turns(f"slack:T0:{ALERT_CHANNEL}:1.0") == ("triage",)


async def test_one_mention_is_one_investigation(monkeypatch):
    from app.slack import handlers

    fake = fake_run_turn("done")
    monkeypatch.setattr(handlers, "run_turn", fake)

    client = FakeClient(messages=[webhook_alert()])
    runtime = SlackRuntime(settings(), identity(), InMemoryDedupe(), turns=TurnLog())
    body = {"event_id": "Ev-dup", "team_id": "T0"}

    first = await run_slack_turn(mention("look"), body, client, runtime)
    second = await run_slack_turn(mention("look"), body, client, runtime)

    assert first and second == ""
    assert len(fake.states) == 1


async def test_a_failing_turn_reports_into_the_thread_and_releases_the_claim(monkeypatch):
    from app.slack import handlers

    async def boom(state_in, *, settings, thread_id, checkpointer=None):
        raise RuntimeError("grafana exploded")
        yield {}

    monkeypatch.setattr(handlers, "run_turn", boom)

    client = FakeClient(messages=[webhook_alert()])
    dedupe = InMemoryDedupe()
    runtime = SlackRuntime(settings(), identity(), dedupe, turns=TurnLog())

    body = {"event_id": "Ev-fail", "team_id": "T0"}
    await handlers._guarded(mention("look"), body, client, runtime)

    posted = client.of("chat_postMessage")
    assert any("That turn failed" in call["text"] for call in posted)
    assert await dedupe.claim("Ev-fail") is True


@pytest.mark.parametrize("channel_type", ["im", "channel"])
def test_thread_id_shape(channel_type):
    runtime = SlackRuntime(settings(), identity(), InMemoryDedupe())
    assert runtime.thread_id("T0", "D0X", "1.0") == "slack:T0:D0X:1.0"


def test_mrkdwn_leaves_dunder_identifiers_alone():
    """`__init__` is not emphasis; only `**` is rewritten."""
    assert to_mrkdwn("call __init__ then __del__") == "call __init__ then __del__"
