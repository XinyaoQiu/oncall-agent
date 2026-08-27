"""`oncall-slackd`: the Socket Mode process.

It owns the WebSocket and nothing else. Spec §2.2: the bolt maintainer's own advice is that
sharing one event loop between a web app and the Socket Mode client is not supported, and
the lifespan workaround breaks outright under `uvicorn --workers > 1`, where every replica
opens its own connection and processes every event N times. So `oncall-api` is a separate
process; each service keeps its own conversation memory, keyed by the Slack thread.

Both identifiers are resolved here, once, from `auth.test` — `bot_id` (`B…`) and
`bot_user_id` (`U…`) — because everything downstream that asks "did we write this" needs the
right one and neither is ever equal to the other (spec §8.4).
"""

import asyncio

from loguru import logger

from app.config import Settings, get_settings
from app.slack.dedupe import build_dedupe
from app.slack.handlers import SlackRuntime, register
from app.slack.thread import resolve_identity

REQUIRED = ("slack_bot_token", "slack_app_token")


def _check(settings: Settings) -> None:
    missing = [name.upper() for name in REQUIRED if not getattr(settings, name)]
    if missing:
        raise SystemExit(f"missing required settings: {', '.join(missing)}")


async def serve(settings: Settings | None = None) -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.app.async_app import AsyncApp

    settings = settings or get_settings()
    _check(settings)

    app = AsyncApp(token=settings.slack_bot_token)
    identity = await resolve_identity(app.client)
    logger.info(f"authenticated as bot_id={identity.bot_id} bot_user_id={identity.bot_user_id}")

    dedupe = build_dedupe(settings)
    await dedupe.setup()

    register(app, SlackRuntime(settings, identity, dedupe))

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    logger.info("oncall-slackd listening")
    await handler.start_async()


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        logger.info("oncall-slackd stopped")


if __name__ == "__main__":
    main()
