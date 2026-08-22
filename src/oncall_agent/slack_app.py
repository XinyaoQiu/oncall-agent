"""Slack entry point: the agent replies when mentioned in an alert thread.

Being summoned rather than posting on every alert is deliberate. The mention itself is a
signal that a human has read the alert and wants help.
"""

import logging
import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import Settings
from .llm import LLMClient, LLMUnavailable
from .models import ThreadMessage
from .pipeline import format_reply, triage
from .sources.knowledge import KnowledgeRepo
from .analysis import writeback

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def _clean(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def build_app(settings: Settings) -> App:
    app = App(token=settings.slack_bot_token)

    def fetch_thread(client, channel: str, thread_ts: str) -> list[ThreadMessage]:
        replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        return [
            ThreadMessage(
                user=m.get("user") or m.get("username", "unknown"),
                text=_clean(m.get("text", "")),
                ts=m["ts"],
                is_bot=bool(m.get("bot_id")),
            )
            for m in replies.get("messages", [])
        ]

    @app.event("app_mention")
    def handle_mention(event, client, say):
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        request = _clean(event.get("text", ""))

        try:
            messages = fetch_thread(client, channel, thread_ts)
        except Exception as exc:
            log.warning("thread fetch failed: %s", exc)
            messages = []

        # The alert itself is the first message; the mention is usually a later reply.
        alert_text = messages[0].text if messages else request

        if _wants_writeback(request):
            _handle_writeback(settings, channel, thread_ts, alert_text, messages, say)
            return

        say(text=":mag: Looking into it…", thread_ts=thread_ts)
        try:
            result = triage(alert_text, messages, question=request or None, settings=settings)
        except LLMUnavailable as exc:
            say(text=f":warning: I can't analyze this right now — {exc}", thread_ts=thread_ts)
            return
        except Exception as exc:
            log.exception("triage failed")
            say(text=f":warning: Triage failed: {exc}", thread_ts=thread_ts)
            return

        say(text=format_reply(result), thread_ts=thread_ts)

    return app


def _wants_writeback(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in ("record this", "write this up", "save this", "resolved"))


def _handle_writeback(settings, channel, thread_ts, alert_text, messages, say) -> None:
    """Draft a knowledge entry and open a PR for review."""
    say(text=":pencil: Drafting a knowledge entry…", thread_ts=thread_ts)

    try:
        llm = LLMClient(settings)
        repo = KnowledgeRepo(settings.knowledge_repo)
        repo.pull()

        from .analysis.identify import identify as identify_alert

        identity = identify_alert(alert_text, llm)
        existing = repo.search_many([identity.alert_name], max_per_term=5)

        entry = writeback.extract(llm, identity.alert_name, messages, None, existing)
        if entry is None:
            say(
                text="I don't think this thread has a durable lesson worth recording.",
                thread_ts=thread_ts,
            )
            return

        url = repo.open_pr(entry, alert_name=identity.alert_name)
        say(text=f":white_check_mark: Drafted for review: {url}", thread_ts=thread_ts)
    except Exception as exc:
        log.exception("writeback failed")
        say(text=f":warning: Could not write this up: {exc}", thread_ts=thread_ts)


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()

    missing = [
        name
        for name, value in (
            ("SLACK_BOT_TOKEN", settings.slack_bot_token),
            ("SLACK_APP_TOKEN", settings.slack_app_token),
            ("GEMINI_API_KEY", settings.gemini_api_key),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    handler = SocketModeHandler(build_app(settings), settings.slack_app_token)
    log.info("oncall-agent listening for mentions")
    handler.start()
