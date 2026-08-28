"""Idempotency on `event_id`.

Slack retries an envelope it did not see acked and redelivers on reconnect, and with ≥2
`slackd` replicas (spec §7.1) duplicate delivery is expected rather than exceptional. Without
a key one @-mention becomes two investigations, two progress messages and two answers.

The claim is deliberately *releasable*. Spec §9's warning is about ordering: a claim taken
before the turn is durable — claim, then enqueue — survives a restart that the turn does
not, and the surviving row blocks the redelivery that would have retried it. So a turn that
dies before it records anything releases its claim, and only a turn that got as far as
answering keeps it. That is the same ordering guarantee with a failure path.
"""

from collections import OrderedDict
from typing import Protocol

from loguru import logger

from app.config import Settings

SCHEMA = """
create table if not exists slack_events (
    event_id   text primary key,
    claimed_at timestamptz not null default now()
)
"""


class Dedupe(Protocol):
    async def setup(self) -> None: ...

    async def claim(self, event_id: str) -> bool: ...

    async def release(self, event_id: str) -> None: ...


class InMemoryDedupe:
    """Per-process claims. Correct for one replica, and honest that it is not shared."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_entries = max_entries

    async def setup(self) -> None:
        return None

    async def claim(self, event_id: str) -> bool:
        if not event_id:
            return True
        if event_id in self._seen:
            return False
        self._seen[event_id] = None
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return True

    async def release(self, event_id: str) -> None:
        self._seen.pop(event_id, None)


class PostgresDedupe:
    """Claims shared across replicas: `INSERT ... ON CONFLICT DO NOTHING`.

    Losing the race means another replica owns the turn, which is the point — the loser
    must not also run it.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def _connect(self):
        import psycopg

        return await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)

    async def setup(self) -> None:
        async with await self._connect() as conn:
            await conn.execute(SCHEMA)

    async def claim(self, event_id: str) -> bool:
        if not event_id:
            return True
        async with await self._connect() as conn:
            cur = await conn.execute(
                "insert into slack_events (event_id) values (%s) on conflict do nothing",
                (event_id,),
            )
            return cur.rowcount == 1

    async def release(self, event_id: str) -> None:
        if not event_id:
            return
        async with await self._connect() as conn:
            await conn.execute("delete from slack_events where event_id = %s", (event_id,))


def build_dedupe(settings: Settings) -> Dedupe:
    if settings.database_url:
        logger.info("slack dedupe: postgres")
        return PostgresDedupe(settings.database_url)
    logger.warning("slack dedupe: in-memory — duplicate delivery is only safe at one replica")
    return InMemoryDedupe()
