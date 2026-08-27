"""Routing — structural, never semantic (spec §6).

The question this module answers is *does this thread contain an alert message*, read from
Slack metadata. It is deliberately not *do I recognise this alert*, and it is never *does
this need investigating*: a classifier that can answer "this one doesn't" is the recognition
gate spec §2.3 rejects, and it fails silently, because a skipped investigation looks exactly
like a thread nobody needed.

So rules 2 and 3 below decide from `bot_id` / `app_id` / channel. An alert the registry has
never seen still routes to `triage`. The keyword table is rule 5, the last resort, for an
alert a human pasted as plain text — the only case with no provenance to read.

`_wants_writeback()` — the old adapter's substring match on "record this" / "resolved" — is
**gone**. A message containing the word "resolved" must never cause a side effect. Write-up
and rating arrive as explicit Block Kit affordances, which is why rule 1 reads `action_id`
and `command` and never message text.
"""

from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from loguru import logger
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.graph.state import Turn
from app.slack.thread import ThreadMessage, to_message

WRITEUP_AFFORDANCES = {"writeup", "write_up", "record"}
RATING_AFFORDANCES = {"rating", "rate", "verdict", "feedback"}


class Decision(BaseModel):
    """The turn, plus which rule produced it — routing decisions are logged, not inferred."""

    turn: Turn
    rule: int
    because: str


class TurnLog:
    """Which turns a thread has already had. Rule 4 (continuity) reads this."""

    def __init__(self, max_threads: int = 4096) -> None:
        self._threads: OrderedDict[str, list[Turn]] = OrderedDict()
        self._max_threads = max_threads

    def record(self, thread_id: str, turn: Turn) -> None:
        turns = self._threads.setdefault(thread_id, [])
        turns.append(turn)
        self._threads.move_to_end(thread_id)
        while len(self._threads) > self._max_threads:
            self._threads.popitem(last=False)

    def turns(self, thread_id: str) -> tuple[Turn, ...]:
        return tuple(self._threads.get(thread_id, ()))


TURNS = TurnLog()


def _normalise(token: str) -> str:
    token = (token or "").strip().lstrip("/").replace("-", "_").lower()
    return token[7:] if token.startswith("oncall_") else token


def affordance(event: dict[str, Any]) -> Turn | None:
    """Rule 1: an explicit Block Kit action or slash command. Ids, never prose."""
    tokens = [a.get("action_id", "") for a in event.get("actions") or [] if isinstance(a, dict)]
    tokens.append(event.get("command") or "")
    tokens.append(event.get("callback_id") or "")
    for token in tokens:
        name = _normalise(token)
        if name in WRITEUP_AFFORDANCES:
            return "writeup"
        if name in RATING_AFFORDANCES:
            return "rating"
    return None


def _root(thread: Sequence[ThreadMessage], event: dict[str, Any]) -> ThreadMessage | None:
    if thread:
        return thread[0]
    if event.get("text") or event.get("bot_id"):
        return to_message(event)
    return None


def decide(
    event: dict[str, Any],
    thread: Sequence[ThreadMessage] = (),
    *,
    settings: Settings | None = None,
    prior_turns: Sequence[str] = (),
) -> Decision:
    """Apply the six rules in order, short-circuiting on the first hit."""
    settings = settings or get_settings()

    explicit = affordance(event)
    if explicit is not None:
        return Decision(turn=explicit, rule=1, because=f"explicit {explicit} affordance")

    root = _root(thread, event)
    if root is not None and not root.is_us:
        if root.bot_id and root.bot_id in set(settings.slack_alert_bot_ids):
            return Decision(
                turn="triage", rule=2, because=f"root bot_id {root.bot_id} is an alert sender"
            )
        if root.app_id and root.app_id in set(settings.slack_alert_app_ids):
            return Decision(
                turn="triage", rule=2, because=f"root app_id {root.app_id} is an alert sender"
            )
        if root.is_bot and event.get("channel") in set(settings.slack_alert_channels):
            return Decision(
                turn="triage",
                rule=3,
                because=f"bot message in alert channel {event.get('channel')}",
            )

    if "triage" in tuple(prior_turns):
        return Decision(turn="followup", rule=4, because="this thread was already triaged")

    if root is not None and root.text:
        from app.domain.alerts import match_alert

        known = match_alert(root.text)
        if known is not None:
            return Decision(turn="triage", rule=5, because=f"root text matches {known.name}")

    return Decision(turn="chat", rule=6, because="no alert message in this thread")


def route(
    event: dict[str, Any],
    thread: Sequence[ThreadMessage] = (),
    *,
    settings: Settings | None = None,
    prior_turns: Sequence[str] = (),
) -> Turn:
    decision = decide(event, thread, settings=settings, prior_turns=prior_turns)
    logger.debug(f"route → {decision.turn} (rule {decision.rule}: {decision.because})")
    return decision.turn
