-- Operational records: one row per invocation, plus the loop steps it took.
--
-- Distinct from rec-knowledge, which holds reviewed conclusions that the agent
-- retrieves. Nothing here is ever retrieved during triage — mixing runs into the
-- knowledge base would bury the one useful entry under every attempt to find it.
--
-- These rows are raw traces with no authority. What a run established reaches the
-- knowledge base only as a candidate entry in a PR (tech design §7.1), where a human
-- merging it is what makes it authoritative. The path out of this table is review,
-- never retrieval.

CREATE TABLE IF NOT EXISTS invocations (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Where it came from, so a Slack thread can be tied back to its run.
    source          TEXT NOT NULL,           -- 'slack' | 'cli'
    channel         TEXT,
    thread_ts       TEXT,
    question        TEXT,

    alert_name      TEXT NOT NULL,
    identified_by   TEXT,                    -- 'rules' | 'llm'
    labels          JSONB NOT NULL DEFAULT '{}'::jsonb,
    deployment      TEXT,

    -- The evidence and the answer, kept whole. Which fields matter for evaluation
    -- is not yet known, and a run cannot be re-created after the fact.
    metrics         JSONB NOT NULL DEFAULT '[]'::jsonb,
    knowledge_hits  JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnosis       JSONB,

    confidence      TEXT,                    -- lifted out of diagnosis for aggregation
    model           TEXT,
    degraded_tier   BOOLEAN NOT NULL DEFAULT false,
    used_sample_data BOOLEAN NOT NULL DEFAULT false,

    investigation_rounds  INT NOT NULL DEFAULT 0,
    stopped_because       TEXT,

    duration_ms     INT,
    error           TEXT,

    -- Filled in later by a human. NULL means nobody has judged this run, which is
    -- different from "it was correct" — the false-confidence rate must not count
    -- unreviewed runs as successes.
    verdict         TEXT,                    -- 'correct' | 'wrong' | 'partial'
    verdict_note    TEXT,
    verdict_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS invocations_created_idx ON invocations (created_at DESC);
CREATE INDEX IF NOT EXISTS invocations_alert_idx   ON invocations (alert_name, created_at DESC);
CREATE INDEX IF NOT EXISTS invocations_verdict_idx ON invocations (verdict)
    WHERE verdict IS NOT NULL;

-- One row per loop round. Separate from invocations because the questions differ:
-- "how did this run go" versus "which searches tend to find the answer".
CREATE TABLE IF NOT EXISTS investigation_steps (
    id              BIGSERIAL PRIMARY KEY,
    invocation_id   BIGINT NOT NULL REFERENCES invocations(id) ON DELETE CASCADE,
    round           INT NOT NULL,
    tool            TEXT NOT NULL,
    args            JSONB NOT NULL DEFAULT '{}'::jsonb,
    reasoning       TEXT,
    observation     TEXT,
    elapsed_ms      INT
);

CREATE INDEX IF NOT EXISTS steps_invocation_idx ON investigation_steps (invocation_id, round);
CREATE INDEX IF NOT EXISTS steps_tool_idx       ON investigation_steps (tool);

-- Labeled cases, built retroactively from postmortems. Prospective note-taking during
-- a rotation does not survive contact with a busy week; existing write-ups do.
CREATE TABLE IF NOT EXISTS labeled_cases (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    alert_name      TEXT NOT NULL,
    alert_text      TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ,
    true_cause      TEXT NOT NULL,
    true_service    TEXT,
    true_files      TEXT[],
    source          TEXT,                    -- postmortem page, incident doc
    notes           TEXT
);
