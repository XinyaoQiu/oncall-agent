"""Reading a Slack thread, and telling who wrote each message.

Routing (spec §6) is decided from *who sent the root message*, not from what it says, so the
classification below is load-bearing: get it wrong and an alert routes to chat, which is the
recognition gate §2.3 exists to prevent.

**`bot_id` (`B…`) and `bot_user_id` (`U…`) are different identifiers.** They are both
"the bot" in conversation and neither is ever equal to the other. The widely-copied
LangChain reference implementation compares one against the other to find its own last
message, so its loop is dead code that never matches. Both are resolved once from
`auth.test` and stored in separate fields here.
"""

import re
from typing import Any

from pydantic import BaseModel, Field

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
DIGEST_LIMIT = 12
DIGEST_CHARS = 400


def clean_text(text: str | None) -> str:
    """Drop the `<@U…>` mention tokens; the address is not part of the question."""
    return _MENTION_RE.sub("", text or "").strip()


class BotIdentity(BaseModel):
    """Our own two identifiers, kept apart on purpose."""

    bot_id: str = ""
    bot_user_id: str = ""
    team_id: str = ""

    def wrote(self, *, bot_id: str | None = None, user: str | None = None) -> bool:
        if bot_id and self.bot_id and bot_id == self.bot_id:
            return True
        return bool(user and self.bot_user_id and user == self.bot_user_id)


async def resolve_identity(client: Any) -> BotIdentity:
    """`auth.test` once at startup. `user_id` is the `U…`; `bot_id` is the `B…`."""
    response = await client.auth_test()
    data = getattr(response, "data", response) or {}
    return BotIdentity(
        bot_id=data.get("bot_id") or "",
        bot_user_id=data.get("user_id") or "",
        team_id=data.get("team_id") or "",
    )


class ThreadMessage(BaseModel):
    """One message, with the metadata routing reads."""

    user: str = ""
    text: str = ""
    ts: str = ""
    is_bot: bool = False
    bot_id: str | None = None
    app_id: str | None = None
    subtype: str | None = None
    username: str = ""
    is_us: bool = False
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def kind(self) -> str:
        """`us` | `webhook_alert` | `bot_alert` | `human` | `unknown` (spec §8.4)."""
        if self.is_us:
            return "us"
        if self.subtype == "bot_message" and self.bot_id and not self.user:
            return "webhook_alert"
        if self.bot_id and self.app_id:
            return "bot_alert"
        if self.bot_id:
            return "bot_alert"
        if self.user:
            return "human"
        return "unknown"

    @property
    def author(self) -> str:
        return self.user or self.username or self.bot_id or "unknown"

    @property
    def is_foreign_bot(self) -> bool:
        """A bot message that is not ours — the shape an alert arrives in."""
        return self.is_bot and not self.is_us


def to_message(raw: dict[str, Any], identity: BotIdentity | None = None) -> ThreadMessage:
    bot_id = raw.get("bot_id")
    user = raw.get("user") or ""
    app_id = raw.get("app_id") or (raw.get("bot_profile") or {}).get("app_id")
    return ThreadMessage(
        user=user,
        text=clean_text(raw.get("text")),
        ts=raw.get("ts") or "",
        is_bot=bool(bot_id) or raw.get("subtype") == "bot_message",
        bot_id=bot_id,
        app_id=app_id,
        subtype=raw.get("subtype"),
        username=raw.get("username") or "",
        is_us=bool(identity and identity.wrote(bot_id=bot_id, user=user)),
        raw=raw,
    )


async def fetch_thread(
    client: Any,
    channel: str,
    ts: str,
    limit: int = 50,
    *,
    identity: BotIdentity | None = None,
) -> list[ThreadMessage]:
    """`conversations.replies`, oldest first. Scopes: `{channels,groups,im,mpim}:history`."""
    response = await client.conversations_replies(channel=channel, ts=ts, limit=limit)
    data = getattr(response, "data", response) or {}
    return [to_message(raw, identity) for raw in (data.get("messages") or [])]


def alert_message(
    messages: list[ThreadMessage], *, match: Any = None
) -> ThreadMessage | None:
    """The thread root, if the root is an alert.

    Structural first: a bot message that is not ours *is* the alert, whether or not the
    registry has ever seen it. The keyword table is only consulted for a root a human
    pasted in, which is the one case with no provenance to read.
    """
    if not messages:
        return None
    root = messages[0]
    if root.is_foreign_bot:
        return root
    if match is None:
        from app.domain.alerts import match_alert as match

    return root if root.text and match(root.text) else None


def thread_digest(messages: list[ThreadMessage], limit: int = DIGEST_LIMIT) -> str:
    """The last `limit` messages as `author: text`, for the planner and the responder.

    Evidence collection never sees this (spec §9 constraint 7); it is context for wording
    and follow-ups, not an input to what gets measured.
    """
    lines = []
    for message in messages[-limit:]:
        text = " ".join(message.text.split())
        if not text:
            continue
        if len(text) > DIGEST_CHARS:
            text = text[:DIGEST_CHARS] + "…"
        lines.append(f"{message.author}: {text}")
    return "\n".join(lines)


def priors(messages: list[ThreadMessage], limit: int = DIGEST_LIMIT) -> list[str]:
    """What the thread already said, root excluded — the root is the alert itself."""
    return [
        f"{m.author}: {' '.join(m.text.split())[:DIGEST_CHARS]}"
        for m in messages[1:][-limit:]
        if m.text.strip()
    ]
