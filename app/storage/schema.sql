-- Operational records: one row per invocation, plus the steps it executed.
--
-- Distinct from the RAG corpus, which holds reviewed conclusions the agent retrieves.
-- Nothing here is ever retrieved during triage (spec §9 item 16) — mixing raw runs into the
-- knowledge base would bury the one useful entry under every attempt to find it. These rows
-- are traces with no authority; what a run established reaches the knowledge base only as a
-- candidate entry in a PR, where a human merging it is what makes it authoritative. The path
-- out of this table is review, never retrieval.

CREATE TABLE IF NOT EXISTS invocations (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Where it came from, so a Slack thread can be tied back to its run.
    source          TEXT NOT NULL,           -- 'slack' | 'api' | 'cli'
    conversation_id TEXT,                    -- slack:{team}:{channel}:{thread_ts}
    channel         TEXT,
    thread_ts       TEXT,
    turn            TEXT,                    -- triage | followup | chat | writeup | rating
    question        TEXT,

    alert_name      TEXT NOT NULL,
    identified_by   TEXT,
    labels          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- 'server-feed' when the lookup was exact, 'server-feed?' when it guessed. A run that
    -- guessed and a run that knew must not aggregate into one number.
    deployment      TEXT,

    -- The evidence and the answer, kept whole. Which fields matter for evaluation is not
    -- yet known, and a run cannot be re-created after the fact.
    observations    JSONB NOT NULL DEFAULT '[]'::jsonb,
    skipped         JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnosis       JSONB,

    -- The reply as it was sent, banners included: a reviewer assigning a verdict judges
    -- what the engineer actually read, not a re-render from today's code.
    response        TEXT,

    confidence      TEXT,                    -- lifted out of diagnosis for aggregation
    degraded_model  TEXT,                    -- the weaker tier that answered, if one did
    used_synthetic  BOOLEAN NOT NULL DEFAULT false,

    steps_taken     INT NOT NULL DEFAULT 0,
    stopped_because TEXT,

    duration_ms     INT,
    error           TEXT,

    -- Filled in later by a human. NULL means nobody has judged this run, which is not the
    -- same as "it was correct": accuracy rates exclude these rows rather than counting them
    -- as successes, or the number improves whenever nobody is checking (spec §9 item 17).
    verdict         TEXT,                    -- 'correct' | 'wrong' | 'partial'
    verdict_note    TEXT,
    verdict_at      TIMESTAMPTZ
);

-- Forward migration from the pre-rewrite schema, which lives in the same database until
-- src/oncall_agent is deleted (spec §11 step 13). CREATE TABLE IF NOT EXISTS is a no-op
-- against those tables, so without this the first index on a renamed column fails and the
-- store disables itself — quietly, since every failure here is swallowed. Additive only:
-- the old columns keep their rows, and this file stays runnable on every start.
ALTER TABLE invocations
    ADD COLUMN IF NOT EXISTS conversation_id TEXT,
    ADD COLUMN IF NOT EXISTS turn            TEXT,
    ADD COLUMN IF NOT EXISTS observations    JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS skipped         JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS response        TEXT,
    ADD COLUMN IF NOT EXISTS degraded_model  TEXT,
    ADD COLUMN IF NOT EXISTS used_synthetic  BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS steps_taken     INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS invocations_created_idx ON invocations (created_at DESC);
CREATE INDEX IF NOT EXISTS invocations_alert_idx   ON invocations (alert_name, created_at DESC);
CREATE INDEX IF NOT EXISTS invocations_thread_idx  ON invocations (conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS invocations_verdict_idx ON invocations (verdict)
    WHERE verdict IS NOT NULL;

-- One row per executed plan step. Separate from invocations because the questions differ:
-- "how did this run go" versus "which searches tend to find the answer".
CREATE TABLE IF NOT EXISTS investigation_steps (
    id              BIGSERIAL PRIMARY KEY,
    invocation_id   BIGINT NOT NULL REFERENCES invocations(id) ON DELETE CASCADE,
    round           INT NOT NULL,
    step            TEXT NOT NULL,
    -- The call signature carries its arguments; `tools` is the names alone, because
    -- aggregating over signatures answers nothing.
    signature       TEXT,
    tools           TEXT[] NOT NULL DEFAULT '{}',
    result          TEXT,
    ok              BOOLEAN NOT NULL DEFAULT true,
    elapsed_ms      INT
);

ALTER TABLE investigation_steps
    ADD COLUMN IF NOT EXISTS step      TEXT,
    ADD COLUMN IF NOT EXISTS signature TEXT,
    ADD COLUMN IF NOT EXISTS tools     TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS result    TEXT,
    ADD COLUMN IF NOT EXISTS ok        BOOLEAN NOT NULL DEFAULT true;

-- The legacy step columns are NOT NULL and the rewrite no longer writes them; on a fresh
-- database they do not exist at all, which is what the handler absorbs.
DO $$
BEGIN
    ALTER TABLE investigation_steps ALTER COLUMN tool DROP NOT NULL;
EXCEPTION WHEN undefined_column THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE investigation_steps ALTER COLUMN args DROP NOT NULL;
EXCEPTION WHEN undefined_column THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS steps_invocation_idx ON investigation_steps (invocation_id, round);
CREATE INDEX IF NOT EXISTS steps_tools_idx      ON investigation_steps USING gin (tools);

-- Slack event idempotency (spec §8.2). Slack retries unacked envelopes and redelivers on
-- reconnect, and with two slackd replicas duplicate delivery is expected rather than
-- exceptional. The claim is an INSERT ... ON CONFLICT DO NOTHING: losing the race means
-- another replica owns the turn.
CREATE TABLE IF NOT EXISTS slack_events (
    event_id        TEXT PRIMARY KEY,
    claimed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS slack_events_claimed_idx ON slack_events (claimed_at);

-- Labeled cases, built retroactively from postmortems. Prospective note-taking during a
-- rotation does not survive contact with a busy week; existing write-ups do.
CREATE TABLE IF NOT EXISTS labeled_cases (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    alert_name      TEXT NOT NULL,
    alert_text      TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ,
    true_cause      TEXT NOT NULL,
    true_service    TEXT,
    true_files      TEXT[],
    source          TEXT,
    notes           TEXT
);
