"""Recording runs, and querying them back.

Writes must never break triage. An agent that fails an incident response because its
telemetry database was down would be worse than one that keeps no records at all, so
every failure here is logged and swallowed.
"""

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class RecordStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._available = True

    @contextmanager
    def _connect(self):
        conn = psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(SCHEMA_PATH.read_text())
            return True
        except Exception as exc:
            log.warning("record store unavailable, running without it: %s", exc)
            self._available = False
            return False

    def record(
        self,
        result,
        *,
        source: str,
        channel: str | None = None,
        thread_ts: str | None = None,
        question: str | None = None,
        duration_ms: int | None = None,
        steps=None,
        error: str | None = None,
    ) -> int | None:
        """Store one run. Returns its id, or None when recording failed."""
        if not self._available:
            return None

        d = result.diagnosis
        inv = result.investigation

        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO invocations (
                        source, channel, thread_ts, question,
                        alert_name, identified_by, labels, deployment,
                        metrics, knowledge_hits, diagnosis,
                        confidence, model, degraded_tier, used_sample_data,
                        investigation_rounds, stopped_because, duration_ms, error
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING id
                    """,
                    (
                        source, channel, thread_ts, question,
                        result.identity.alert_name,
                        result.identity.identified_by,
                        json.dumps(result.identity.labels),
                        _deployment_label(result),
                        json.dumps([
                            {"query": m.query, "source": m.source,
                             "series": len(m.series), "error": m.error}
                            for m in result.metrics
                        ]),
                        json.dumps([
                            {"path": h.path, "line": h.line_number, "term": h.matched_term}
                            for h in result.knowledge_hits
                        ]),
                        json.dumps(d.model_dump()) if d else None,
                        d.confidence.value if d else None,
                        d.model if d else None,
                        bool(d and d.degraded_tier),
                        any(m.source == "sample" for m in result.metrics),
                        inv.rounds if inv else 0,
                        inv.stopped_because if inv else None,
                        duration_ms,
                        error,
                    ),
                ).fetchone()

                invocation_id = row["id"]
                for step in steps or []:
                    conn.execute(
                        """
                        INSERT INTO investigation_steps (
                            invocation_id, round, tool, args, reasoning, observation, elapsed_ms
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            invocation_id, step.round, step.tool, json.dumps(step.args),
                            step.reasoning, step.observation, int(step.elapsed * 1000),
                        ),
                    )
                return invocation_id
        except Exception as exc:
            log.warning("failed to record invocation: %s", exc)
            return None

    def set_verdict(self, invocation_id: int, verdict: str, note: str | None = None) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE invocations
                       SET verdict = %s, verdict_note = %s, verdict_at = now()
                     WHERE id = %s
                    """,
                    (verdict, note, invocation_id),
                )
            return True
        except Exception as exc:
            log.warning("failed to set verdict: %s", exc)
            return False

    def recent(self, limit: int = 20, alert_name: str | None = None) -> list[dict]:
        sql = """
            SELECT id, created_at, alert_name, confidence, model, degraded_tier,
                   investigation_rounds, stopped_because, duration_ms, verdict, error
              FROM invocations
             {where}
             ORDER BY created_at DESC
             LIMIT %s
        """.format(where="WHERE alert_name = %s" if alert_name else "")
        params = (alert_name, limit) if alert_name else (limit,)

        try:
            with self._connect() as conn:
                return conn.execute(sql, params).fetchall()
        except Exception as exc:
            log.warning("query failed: %s", exc)
            return []

    def replay(self, invocation_id: int) -> dict | None:
        """Everything about one run, for working out why it went the way it did."""
        try:
            with self._connect() as conn:
                run = conn.execute(
                    "SELECT * FROM invocations WHERE id = %s", (invocation_id,)
                ).fetchone()
                if not run:
                    return None
                run["steps"] = conn.execute(
                    """
                    SELECT round, tool, args, reasoning, observation, elapsed_ms
                      FROM investigation_steps
                     WHERE invocation_id = %s
                     ORDER BY round
                    """,
                    (invocation_id,),
                ).fetchall()
                return run
        except Exception as exc:
            log.warning("replay failed: %s", exc)
            return None

    def thread_history(self, thread_ts: str, limit: int = 5) -> list[dict]:
        """Earlier investigations in this Slack thread, oldest first.

        A thread is a conversation: the second mention is usually a follow-up to the
        first, and re-deriving what was already established wastes the engineer's time
        and the budget. The rows are already here for evaluation; this reads them back.
        """
        if not self._available:
            return []
        try:
            with self._connect() as conn:
                runs = conn.execute(
                    """
                    SELECT id, created_at, alert_name, confidence, diagnosis,
                           investigation_rounds, stopped_because
                      FROM invocations
                     WHERE thread_ts = %s
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (thread_ts, limit),
                ).fetchall()

                for run in runs:
                    run["steps"] = conn.execute(
                        """
                        SELECT tool, args, observation
                          FROM investigation_steps
                         WHERE invocation_id = %s
                         ORDER BY round
                        """,
                        (run["id"],),
                    ).fetchall()
                return list(reversed(runs))
        except Exception as exc:
            log.warning("thread history lookup failed: %s", exc)
            return []

    def stats(self, days: int = 30) -> dict:
        """The numbers §9 asks for.

        Unreviewed runs are excluded from the confidence figures rather than counted as
        correct — otherwise the rate improves simply because nobody is checking.
        """
        try:
            with self._connect() as conn:
                overall = conn.execute(
                    """
                    SELECT count(*)                                        AS runs,
                           count(*) FILTER (WHERE error IS NOT NULL)       AS failed,
                           count(*) FILTER (WHERE degraded_tier)           AS degraded,
                           count(*) FILTER (WHERE used_sample_data)        AS on_sample_data,
                           count(*) FILTER (WHERE verdict IS NOT NULL)     AS reviewed,
                           round(avg(duration_ms))                         AS avg_ms,
                           round(avg(investigation_rounds), 1)             AS avg_rounds
                      FROM invocations
                     WHERE created_at > now() - make_interval(days => %s)
                    """,
                    (days,),
                ).fetchone()

                confidence = conn.execute(
                    """
                    SELECT confidence,
                           count(*)                                      AS n,
                           count(*) FILTER (WHERE verdict = 'wrong')     AS wrong,
                           count(*) FILTER (WHERE verdict IS NOT NULL)   AS reviewed
                      FROM invocations
                     WHERE created_at > now() - make_interval(days => %s)
                       AND confidence IS NOT NULL
                     GROUP BY confidence
                    """,
                    (days,),
                ).fetchall()

                tools = conn.execute(
                    """
                    SELECT s.tool, count(*) AS n
                      FROM investigation_steps s
                      JOIN invocations i ON i.id = s.invocation_id
                     WHERE i.created_at > now() - make_interval(days => %s)
                     GROUP BY s.tool
                     ORDER BY n DESC
                    """,
                    (days,),
                ).fetchall()

                return {"overall": overall, "by_confidence": confidence, "tools": tools}
        except Exception as exc:
            log.warning("stats failed: %s", exc)
            return {}


def _deployment_label(result) -> str | None:
    """What the run resolved to, with its standing — 'server-feed' vs 'server-feed?'.

    A run that guessed and a run that knew must not aggregate into the same number.
    """
    res = getattr(result, "resolution", None)
    if res is None or not res.deployment:
        return None
    return res.deployment.app_label + ("" if res.is_confident else "?")


def open_store(dsn: str | None) -> RecordStore | None:
    """Open and initialise the store, or return None to run without recording."""
    if not dsn:
        return None
    store = RecordStore(dsn)
    return store if store.ensure_schema() else None
