"""Recording runs, and querying them back.

Ported from `src/oncall_agent/storage/records.py` onto `psycopg.AsyncConnection`. The old
store used `psycopg.connect`, which on the Socket Mode event loop blocks the WebSocket for
as long as the database takes (spec §2.1); telemetry stalling the reply it is telemetry
*about* is the wrong way round.

Two properties are load-bearing and survive the port unchanged.

**Writes must never break triage.** An agent that fails an incident response because its
telemetry database was down is worse than one that keeps no records at all, so every failure
here is logged and swallowed. `open_store()` returning `None` is the normal unconfigured
case, and every caller treats a missing store as a no-op.

**`verdict IS NULL` means UNREVIEWED, never correct** (spec §9 item 17). Accuracy figures
are computed over reviewed rows only. Folding unreviewed runs into the denominator as
successes makes the number improve whenever nobody is checking, which is precisely backwards.
"""

import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
from loguru import logger
from psycopg.rows import dict_row

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
CONNECT_TIMEOUT = 5

VERDICTS = ("correct", "wrong", "partial")

# ExecutedStep.tool holds the call signatures this step made, joined by this separator.
SIGNATURE_SEP = " | "


def tool_names(signature: str | None) -> list[str]:
    """The tool names inside a call signature, without their arguments.

    `list_dir({"repo": "server"})` aggregates with nothing; `list_dir` aggregates with every
    other listing, which is the question the steps table exists to answer.
    """
    names = []
    for part in (signature or "").split(SIGNATURE_SEP):
        name = part.strip().split("(", 1)[0].strip()
        if name:
            names.append(name)
    return names


def deployment_label(state: Mapping[str, Any]) -> str | None:
    """What the run resolved to, with its standing — 'server-feed' vs 'server-feed?'.

    A run that guessed and a run that knew must not aggregate into the same number: the
    guess's pod and replica queries succeed and return true data about the wrong workload,
    so a bare label would make the two indistinguishable in every aggregate afterwards.
    """
    resolution = state.get("resolution")
    label = getattr(resolution, "app_label", None)
    if not label:
        return None
    return label if getattr(resolution, "is_confident", False) else f"{label}?"


def _observations(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for observation in state.get("baseline") or []:
        out.append(
            {
                "query": observation.query,
                "purpose": observation.purpose,
                "source": observation.source,
                "series": len(observation.series),
                "empty": observation.is_empty,
                "error": observation.error,
                "caveats": observation.all_caveats(),
            }
        )
    return out


def _skipped(state: Mapping[str, Any]) -> list[dict[str, str]]:
    return [{"probe": s.probe, "reason": s.reason} for s in state.get("skipped") or []]


def _diagnosis(state: Mapping[str, Any]) -> tuple[str | None, str | None]:
    diagnosis = state.get("diagnosis")
    if diagnosis is None:
        return None, None
    return json.dumps(diagnosis.model_dump(), ensure_ascii=False), diagnosis.confidence


class RecordStore:
    """One Postgres DSN's worth of operational records. Every method degrades to a no-op."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    @asynccontextmanager
    async def _connect(self):
        conn = await psycopg.AsyncConnection.connect(
            self.dsn, row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT
        )
        try:
            yield conn
            await conn.commit()
        finally:
            await conn.close()

    async def ping(self) -> str | None:
        """`None` when the database answered, otherwise why it did not."""
        try:
            async with self._connect() as conn:
                await conn.execute("SELECT 1")
            return None
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"

    async def ensure_schema(self) -> bool:
        try:
            async with self._connect() as conn:
                await conn.execute(SCHEMA_PATH.read_text())
            return True
        except Exception as exc:
            logger.warning(f"record store unavailable, running without it: {exc}")
            self._available = False
            return False

    async def record(
        self,
        state: Mapping[str, Any],
        *,
        source: str,
        conversation_id: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
        question: str | None = None,
        duration_ms: int | None = None,
        rounds: int | None = None,
        error: str | None = None,
    ) -> int | None:
        """Store one run and its executed steps. Returns the invocation id, or `None`.

        `state` is an `OncallState` or any mapping shaped like one — the SSE adapter has only
        what its event stream carried, and a thinner row is better than a fabricated one.
        """
        if not self._available:
            return None

        identity = state.get("identity")
        steps = list(state.get("past_steps") or [])
        diagnosis_json, confidence = _diagnosis(state)
        alert_name = (
            getattr(identity, "alert_name", None) or state.get("alert_name") or "unknown"
        )

        try:
            async with self._connect() as conn:
                cursor = await conn.execute(
                    """
                    INSERT INTO invocations (
                        source, conversation_id, channel, thread_ts, turn, question,
                        alert_name, identified_by, labels, deployment,
                        observations, skipped, diagnosis, response,
                        confidence, degraded_model, used_synthetic,
                        steps_taken, stopped_because, duration_ms, error
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING id
                    """,
                    (
                        source,
                        conversation_id or state.get("conversation_id"),
                        channel,
                        thread_ts,
                        state.get("turn"),
                        question or state.get("input"),
                        alert_name,
                        getattr(identity, "identified_by", None),
                        json.dumps(getattr(identity, "labels", None) or {}, ensure_ascii=False),
                        deployment_label(state),
                        json.dumps(_observations(state), ensure_ascii=False),
                        json.dumps(_skipped(state), ensure_ascii=False),
                        diagnosis_json,
                        state.get("response"),
                        confidence,
                        state.get("degraded_model"),
                        bool(state.get("used_synthetic")),
                        rounds if rounds is not None else len(steps),
                        state.get("stopped_because"),
                        duration_ms,
                        error,
                    ),
                )
                row = await cursor.fetchone()
                invocation_id = row["id"]

                if steps:
                    async with conn.cursor() as cursor:
                        await cursor.executemany(
                            """
                            INSERT INTO investigation_steps (
                                invocation_id, round, step, signature, tools,
                                result, ok, elapsed_ms
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            [
                                (
                                    invocation_id,
                                    index,
                                    step.step,
                                    step.tool,
                                    tool_names(step.tool),
                                    step.result,
                                    step.ok,
                                    step.elapsed_ms,
                                )
                                for index, step in enumerate(steps, start=1)
                            ],
                        )
                return invocation_id
        except Exception as exc:
            logger.warning(f"failed to record invocation: {exc}")
            return None

    async def claim_event(self, event_id: str) -> bool:
        """Claim a Slack event id. `False` means someone else already owns this delivery.

        Spec §8.2: the claim happens *after* the turn is durably recorded. Claiming first and
        then enqueuing is what makes a restart drop the turn permanently — the dedupe row
        survives and blocks the redelivery that would have retried it.
        """
        if not self._available:
            return True
        try:
            async with self._connect() as conn:
                cursor = await conn.execute(
                    "INSERT INTO slack_events (event_id) VALUES (%s) "
                    "ON CONFLICT DO NOTHING RETURNING event_id",
                    (event_id,),
                )
                return await cursor.fetchone() is not None
        except Exception as exc:
            logger.warning(f"event dedupe unavailable for {event_id}: {exc}")
            return True

    async def set_verdict(self, invocation_id: int, verdict: str, note: str | None = None) -> bool:
        if verdict not in VERDICTS:
            logger.warning(f"refusing unknown verdict {verdict!r}")
            return False
        try:
            async with self._connect() as conn:
                cursor = await conn.execute(
                    """
                    UPDATE invocations
                       SET verdict = %s, verdict_note = %s, verdict_at = now()
                     WHERE id = %s
                    RETURNING id
                    """,
                    (verdict, note, invocation_id),
                )
                return await cursor.fetchone() is not None
        except Exception as exc:
            logger.warning(f"failed to set verdict: {exc}")
            return False

    async def recent(self, limit: int = 20, alert_name: str | None = None) -> list[dict]:
        where = "WHERE alert_name = %s" if alert_name else ""
        sql = f"""
            SELECT id, created_at, source, alert_name, deployment, confidence,
                   degraded_model, used_synthetic, steps_taken, stopped_because,
                   duration_ms, verdict, error
              FROM invocations
             {where}
             ORDER BY created_at DESC
             LIMIT %s
        """
        params = (alert_name, limit) if alert_name else (limit,)
        try:
            async with self._connect() as conn:
                cursor = await conn.execute(sql, params)
                return await cursor.fetchall()
        except Exception as exc:
            logger.warning(f"query failed: {exc}")
            return []

    async def replay(self, invocation_id: int) -> dict | None:
        """Everything about one run, for working out why it went the way it did."""
        try:
            async with self._connect() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM invocations WHERE id = %s", (invocation_id,)
                )
                run = await cursor.fetchone()
                if not run:
                    return None
                cursor = await conn.execute(
                    """
                    SELECT round, step, signature, tools, result, ok, elapsed_ms
                      FROM investigation_steps
                     WHERE invocation_id = %s
                     ORDER BY round
                    """,
                    (invocation_id,),
                )
                run["steps"] = await cursor.fetchall()
                return run
        except Exception as exc:
            logger.warning(f"replay failed: {exc}")
            return None

    async def thread_history(self, conversation_id: str, limit: int = 5) -> list[dict]:
        """Earlier runs in this conversation, oldest first.

        A thread is a conversation: the second mention is usually a follow-up to the first,
        and re-deriving what was already established wastes the engineer's time and the
        budget. These reach a turn as `state["priors"]`, which the evidence layer cannot
        read — priors reorder work downstream, they never remove a measurement.
        """
        if not self._available:
            return []
        try:
            async with self._connect() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id, created_at, alert_name, confidence, diagnosis,
                           steps_taken, stopped_because
                      FROM invocations
                     WHERE conversation_id = %s
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (conversation_id, limit),
                )
                runs = await cursor.fetchall()
                return list(reversed(runs))
        except Exception as exc:
            logger.warning(f"thread history lookup failed: {exc}")
            return []

    async def stats(self, days: int = 30) -> dict:
        """The numbers §9 asks for, with unreviewed runs kept out of every rate.

        `reviewed` is reported next to `runs` so the reader can see how much of the window
        anyone actually judged; `by_confidence` returns raw counts rather than a percentage
        for the same reason — a rate over two reviewed runs is not a rate.
        """
        try:
            async with self._connect() as conn:
                cursor = await conn.execute(
                    """
                    SELECT count(*)                                          AS runs,
                           count(*) FILTER (WHERE error IS NOT NULL)         AS failed,
                           count(*) FILTER (WHERE degraded_model IS NOT NULL) AS degraded,
                           count(*) FILTER (WHERE used_synthetic)            AS on_synthetic,
                           count(*) FILTER (WHERE verdict IS NOT NULL)       AS reviewed,
                           count(*) FILTER (WHERE verdict IS NULL)           AS unreviewed,
                           round(avg(duration_ms))::int                      AS avg_ms,
                           round(avg(steps_taken), 1)::float                 AS avg_steps
                      FROM invocations
                     WHERE created_at > now() - make_interval(days => %s)
                    """,
                    (days,),
                )
                overall = await cursor.fetchone()

                cursor = await conn.execute(
                    """
                    SELECT confidence,
                           count(*)                                    AS n,
                           count(*) FILTER (WHERE verdict IS NOT NULL) AS reviewed,
                           count(*) FILTER (WHERE verdict = 'correct') AS correct,
                           count(*) FILTER (WHERE verdict = 'wrong')   AS wrong,
                           count(*) FILTER (WHERE verdict = 'partial') AS partial
                      FROM invocations
                     WHERE created_at > now() - make_interval(days => %s)
                       AND confidence IS NOT NULL
                     GROUP BY confidence
                    """,
                    (days,),
                )
                confidence = await cursor.fetchall()

                cursor = await conn.execute(
                    """
                    SELECT t AS tool, count(*) AS n
                      FROM investigation_steps s
                      JOIN invocations i ON i.id = s.invocation_id
                      CROSS JOIN LATERAL unnest(s.tools) AS t
                     WHERE i.created_at > now() - make_interval(days => %s)
                     GROUP BY t
                     ORDER BY n DESC
                    """,
                    (days,),
                )
                tools = await cursor.fetchall()

                return {"overall": overall, "by_confidence": confidence, "tools": tools}
        except Exception as exc:
            logger.warning(f"stats failed: {exc}")
            return {}


async def open_store(dsn: str | None) -> RecordStore | None:
    """Open and initialise the store, or `None` to run without recording."""
    if not dsn:
        logger.info("no DATABASE_URL configured; runs will not be recorded")
        return None
    store = RecordStore(dsn)
    return store if await store.ensure_schema() else None
