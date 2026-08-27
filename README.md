# oncall-agent

An on-call triage assistant. It lives in Slack: invite it to an alert channel, @-mention it
in a thread, and it reads the alert, pulls the authoritative rule and metrics, searches past
incidents and the code, and replies with a diagnosis. You can also just talk to it — ask
what a service does or what an error code means — and it answers without turning the
question into an investigation.

Design: [docs/tech-design.md](docs/tech-design.md) ·
Rewrite spec: [docs/superpowers/specs/2026-08-26-langgraph-slack-rewrite.md](docs/superpowers/specs/2026-08-26-langgraph-slack-rewrite.md)

## How it works

```
@oncall-agent in a Slack thread
   │
   ├─ is there an alert here?              code — reads Slack metadata, not the message text
   │     no ─────────────────────────────► chat: tool-calling, bounded rounds
   │     yes
   ├─ is this message about diagnosing it?  model — consulted only when an alert exists
   │     "what is server-feed?" ──────────► chat, and the reply says it did not investigate
   │     anything else, or unsure ────────► triage
   ▼
 baseline      the deterministic floor. no model. the only edge into the planner.
   │           the alert's own query first, then deployment, golden signals, ingress impact
   ▼
 planner       reads the floor and retrieved runbooks; emits a step list
   ▼
 executor  ⇄  replanner        continue | replan | respond, with budgets in code
   ▼
 respond       evidence, then a confidence-tagged hypothesis, with every banner it owes you
```

Four ideas run through it.

**The Slack message identifies the alert; it never supplies the numbers.** Rendered alert
text rounds values and drops labels. A wrong identification fails loudly at rule lookup; a
wrong number fails silently and ends up in the incident review as fact.

**Correctness lives in signatures, not prompts.** `quantify_impact()` has no `source`
parameter, so counting user impact from a log sampled at 1/8 is not forbidden — it is
inexpressible. `collect_baseline(identity, rule, resolution, *, minutes)` cannot accept the
engineer's question or the thread priors, so "the question shapes emphasis, never
collection" is enforced by a signature with a test asserting it.

**The floor is on the only path in.** Adopting a planning agent means the model decides the
order of work — which is the point, and also the risk: anything it schedules, it can
deschedule. So the deterministic evidence floor is not a step the planner ranks first. It is
a node the planner cannot reach around.

**A degraded input degrades the output with a label attached.** A reconstructed query, a
guessed deployment, fixture data instead of live metrics — each is usable when it says what
it is. The danger was never the weaker input; it was the weaker input rendering identically
to the strong one.

## Quick start

```bash
uv sync
make up                    # postgres + milvus
cp .env.example .env       # then fill in what you have

# the deterministic half — no model, no API key, no services
uv run oncall evidence "[FIRING] news-list-for-channel p99 app=server-feed"
```

That last command is the one that works with nothing configured. It prints the evidence
floor and nothing else: which rung of the ladder the rule came from, how confident the
deployment resolution is, and — importantly — how many queries were issued, returned data,
came back empty, and failed. Those are four different numbers.

## Running it

```bash
make mcp        # Grafana MCP server on :8005 (serves flagged fixtures without GRAFANA_URL)
make api        # HTTP + SSE on :9900
make slackd     # the Slack bot, Socket Mode
make check      # ruff + import contracts + tests
```

`oncall-api` and `oncall-slackd` are **two processes on purpose**. bolt-python's maintainer
advises against sharing an event loop between a web app and a WebSocket client, and the
common lifespan workaround breaks under `uvicorn --workers > 1` — each worker opens its own
socket and handles every Slack event again.

## Configuration

Everything degrades rather than crashes, and `/health` says which of these is missing and
what that costs you.

| Variable | Without it |
|---|---|
| `DASHSCOPE_API_KEY` (or `OPENAI_API_KEY`) | Triage returns an error. An unavailable model is an error, not a degraded mode — a reply that silently omits half its analysis renders identically to a complete one |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | No Slack; the CLI and HTTP surfaces still work |
| `SLACK_ALERT_BOT_IDS` | Alert threads are only recognised by keyword, not by provenance |
| `GRAFANA_URL` | The MCP server serves fixtures, flagged as synthetic everywhere they appear |
| `DATABASE_URL` | No run records, no verdicts, no Slack event dedupe |
| `MILVUS_HOST` | No runbook retrieval; code search still works |

## Slack app setup

1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. **Socket Mode** → enable → app-level token with `connections:write` → `SLACK_APP_TOKEN`
3. **OAuth & Permissions** → bot scopes: `app_mentions:read`, `channels:history`,
   `groups:history`, `im:history`, `mpim:history`, `chat:write`, `chat:write.public`,
   `commands`
4. **Event Subscriptions** → subscribe to `app_mention` and `message.im`
5. **Install to Workspace** → bot token → `SLACK_BOT_TOKEN`
6. `/invite @oncall-agent` in your alert channel
7. Put your alerting bot's ID in `SLACK_ALERT_BOT_IDS` — routing reads provenance, so this
   is what lets an alert nobody has catalogued still get the full evidence floor

Socket Mode dials out, so there is no public URL and no ingress. That matters here: the
agent's tools reach Grafana, Milvus and internal MCP servers, so the process has to live
inside the network anyway.

## Two stores, one direction

`rec-knowledge` holds reviewed conclusions the agent retrieves during triage. Postgres holds
one row per invocation — which queries ran, what each round did, what was concluded at what
confidence, and whether a human later corrected it.

Runs are **never retrieved**. Burying the one useful entry under every attempt to find it is
how a knowledge base degrades. The path out of the run store is review — a PR — never
retrieval.

```bash
uv run oncall runs
uv run oncall replay 12
uv run oncall verdict 12 wrong --note "was actually a DNS failure"
uv run oncall stats --days 30
```

`verdict` is how accuracy gets measured rather than assumed. Unreviewed runs count as
unreviewed, never as correct — otherwise the false-confidence rate improves whenever nobody
is checking.

## Layout

```
app/
├── domain/      facts: deployment shards, alert registry, repo list, source contracts
├── evidence/    deterministic measurement. no model calls, ever.
├── graph/       the LangGraph: nodes, guards, state
├── tools/       local + MCP tools, wrapped with provenance at load
├── render/      the one renderer. every banner is computed here.
├── slack/       Socket Mode adapter: routing, threads, progress, dedupe
├── api/         FastAPI: health, SSE triage, admin
└── storage/     run records (async psycopg)
mcp_servers/     Grafana, and the ported fixtures — where synchronous I/O is allowed to live
config/          the domain tables, as data
```

Five import contracts in `.importlinter` keep that layering from decaying into an
intention: domain may not import the graph, the graph may not read the record store,
adapters may not reach past `build_graph` into a node, and no synchronous client may be
constructed inside `app/graph` or `app/evidence`.

## Tests

```bash
uv run pytest
```

The suite covers everything that does not need a model. The load-bearing ones are in
§9 of the rewrite spec: a table mapping each guarantee to the mechanism that enforces it and
the test name that fails when it decays. `test_baseline_signature_is_frozen` is the shape of
the whole idea — it asserts a function signature, so widening the evidence floor's inputs
fails CI rather than a code review.
