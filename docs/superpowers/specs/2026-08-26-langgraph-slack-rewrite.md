# LangGraph + Slack Rewrite — Design Spec

**Date:** 2026-08-26
**Branch:** `feature/langgraph-slack-rewrite`
**Supersedes:** the hand-rolled pipeline in `src/oncall_agent/` (`pipeline.triage()` + `investigate/loop.py`)

The agent moves from a fixed pipeline with an agentic sub-loop to a **LangGraph
Plan-Execute-Replan graph over MCP tools**, and Slack becomes a first-class entry point:
the bot is invited into alert channels, engineers @-mention it, it reads the alert and the
whole thread, and it also holds ordinary conversations there.

The architecture is adopted from the sibling project at `../oncall-agent-new`
(FastAPI + LangChain/LangGraph + Milvus RAG + MCP). What this repo contributes back is the
part that project has no way to have: **production domain knowledge, and the discipline of
keeping correctness in code rather than in prompts.**

---

## 1. What changes, and what must not

| | Before | After |
|---|---|---|
| Orchestration | `pipeline.triage()`, fixed 8-step sequence | LangGraph `StateGraph`: baseline → planner → executor → replanner |
| Iteration | `investigate/loop.py`, hand-rolled action selector | Plan-Execute-Replan with re-planning |
| Tools | 5 hardcoded lambdas in `build_toolset()` | Local LangChain tools + MCP servers, loaded at runtime |
| Knowledge retrieval | ripgrep over `rec-knowledge` | Milvus vector search **and** ripgrep — different corpora, §7.3 |
| Entry points | CLI + a synchronous Slack bot | `oncall-slackd` (Socket Mode) + `oncall-api` (FastAPI/SSE) + `oncall` (CLI) |
| Model | Gemini via `google-genai` | Configurable via a factory; DashScope/Qwen is the default the sibling repo uses |
| Session memory | Postgres rows read back by `memory.summarize()` | LangGraph checkpointer keyed on the Slack thread |

**What must not change** is the reason this repo exists. Twenty-three domain assets are
catalogued in §9. They share one signature: *the failure renders identically to success.*
A wrong workload's pod query returns true data. A 1/8-sampled count looks authoritative.
An empty result looks healthy. None of these announce themselves, so none of them can be
left to a prompt.

---

## 2. Three findings that shaped this design

Three candidate architectures were written and each was reviewed by three adversarial
judges (feasibility, correctness, Slack UX). None scored above 5/10 on first pass. The
flaws they found are the load-bearing constraints here.

### 2.1 The existing domain layer is entirely synchronous

`grep -c 'async def' src/` returns **0** across ~2,400 lines. `sources/grafana.py` uses
`httpx.Client`, `storage/records.py` uses `psycopg.connect`, `sources/knowledge.py` uses
`subprocess.run`. Every proposal put this code inside async LangGraph nodes running on the
Socket Mode event loop, where a 30-second Grafana timeout stalls the WebSocket, Slack stops
receiving acks, and the app gets throttled off new events.

**Resolution (§5):** synchronous I/O moves into MCP server processes, where blocking is
local and harmless. Pure computation with no I/O — `resolve()`, `match_alert()` — has
nothing to block on and stays in-process. Anything that must do I/O in-process is
explicitly `await asyncio.to_thread(...)`, and an import-linter contract fails the build if
a sync client is constructed inside `app/graph/`.

### 2.2 Sharing one event loop between Slack and FastAPI is not supported

The bolt-python maintainer states it directly: *"I really don't recommend sharing a single
event-loop for several purposes (= Web app and WebSocket client)... having two separate apps
is the only way that I suggest."* The community lifespan workaround also breaks with
`uvicorn --workers > 1` — each worker opens its own WebSocket and every Slack event is
processed N times.

**Resolution (§4):** two processes. `oncall-slackd` owns the Socket Mode connection;
`oncall-api` owns HTTP and can scale workers freely. They share a compiled graph definition
and a Postgres checkpointer, not a process.

### 2.3 A "chat vs triage" classifier is the recognition gate §1.1 forbids

Every proposal routed inbound mentions through a classifier that could answer "this one
doesn't need an investigation" — and in two of them, chat was the fall-through default. That
is precisely the failure §1.1 exists to prevent, reintroduced invisibly and with no trace.

**Resolution (§6), revised during implementation.** The first fix was to make routing
*purely* structural: does this thread contain an alert message, answered from Slack metadata
rather than from the text. That half is right and it stays — it means an alert nobody has
catalogued still gets the evidence floor.

But purely structural over-triggers. "What is server-feed?" typed into an alert thread would
buy six metric queries and a plan-execute loop to answer a definitional question. So there
are two gates: **structure decides whether an alert exists (code), intent decides what was
asked about it (model)** — and the second gate is made safe not by being avoided but by
being biased and visible. Uncertainty resolves to investigate, in code; a chat answer inside
an alert thread says so and offers to upgrade in one word.

The distinction that matters: §1.1 forbids gating on *recognition* ("I don't know this
alert, so I won't look"). It does not forbid noticing that the engineer asked something
else. Conflating the two is what made the first version too rigid.

---

## 3. Repository layout

```
oncall-agent/
├── pyproject.toml               # 3 console_scripts: oncall-slackd, oncall-api, oncall
├── docker-compose.yml           # milvus(+etcd,minio,attu) + postgres
├── Makefile
├── .importlinter                # layer contracts (§5.3)
├── config/
│   ├── deployments.yaml         # the 4 server-* shards  (was config.py:DEPLOYMENTS)
│   ├── alerts.yaml              # KNOWN_ALERTS + benign_patterns  (was alerts.py)
│   ├── repos.yaml               # multi-repo registry     (was repos.py)
│   └── sources.yaml             # the §5.1 sampling/retention trap table, as data
├── app/
│   ├── config.py                # pydantic-settings, no import-time I/O
│   ├── domain/                  # facts. no I/O, no LLM. importable from anywhere.
│   │   ├── deployments.py       #   Resolution, resolve(), resolve_blast_radius()
│   │   ├── alerts.py            #   match_alert(), get_alert(), BenignPattern
│   │   ├── repos.py             #   RepoRegistry.rank_for()  — ranks, never filters
│   │   └── sources.py           #   SourceContract, SOURCES
│   ├── evidence/                # deterministic measurement. NO thread-context params.
│   │   ├── envelope.py          #   Observation + render(): the provenance envelope
│   │   ├── identify.py          #   rules-first alert identification
│   │   ├── rules.py             #   fetch_rule() + the 4-rung provenance ladder
│   │   ├── baseline.py          #   collect_baseline(identity, rule, resolution, *, minutes)
│   │   ├── impact.py            #   quantify_impact() — ingress baked in, no source param
│   │   └── accounting.py        #   queries / with-data / empty / failed
│   ├── graph/
│   │   ├── state.py             #   OncallState + reducers
│   │   ├── build.py             #   build_graph() — compiled once
│   │   ├── nodes/               #   baseline, planner, executor, replanner, respond
│   │   ├── guards.py            #   budget, dedupe, required-args, impact-source
│   │   └── checkpoint.py        #   AsyncPostgresSaver wiring
│   ├── tools/
│   │   ├── registry.py          #   local + MCP merge, provenance wrapping (§7.2)
│   │   ├── knowledge.py         #   Milvus retrieval  (RAG)
│   │   ├── code_search.py       #   ripgrep over the repo registry (lexical, §7.3)
│   │   └── mcp_client.py        #   MultiServerMCPClient + retry interceptor
│   ├── render/
│   │   └── answer.py            #   THE renderer. banners + accounting. one implementation.
│   ├── slack/
│   │   ├── run.py               #   oncall-slackd entry point
│   │   ├── handlers.py          #   app_mention / message.im, ack-then-background
│   │   ├── router.py            #   structural routing (§6)
│   │   ├── thread.py            #   conversations.replies → ThreadMessage[], alert detection
│   │   ├── progress.py          #   batched progress writer (§8.3)
│   │   ├── mrkdwn.py            #   Markdown → Slack mrkdwn
│   │   └── dedupe.py            #   event_id idempotency
│   ├── api/                     #   FastAPI: health, sse, admin(runs/verdict/replay)
│   ├── storage/                 #   ops records (async psycopg) + schema.sql
│   └── main.py                  #   oncall-api entry point
├── mcp_servers/
│   ├── grafana_server.py        #   rule lookup + PromQL   (owns the sync httpx client)
│   ├── logs_server.py           #   ES/Loki, provenance-wrapped   (§7.2)
│   ├── monitor_server.py        #   ported from ../oncall-agent-new (synthetic — flagged)
│   └── cls_server.py            #   ported from ../oncall-agent-new (synthetic — flagged)
└── tests/
```

`src/oncall_agent/` is deleted at the end of the migration, not the start (§11).

---

## 4. Process model

```
┌── oncall-slackd ────────┐   ┌── oncall-api (uvicorn -w N) ─┐
│ AsyncApp                │   │ FastAPI: /health /sse /admin │
│ AsyncSocketModeHandler  │   │ static web UI                │
│ ack ≤3s → asyncio.Task  │   └──────────────────────────────┘
└─────────────────────────┘                 │
              │  both import app.graph.build_graph()
              ▼                             ▼
      ┌──────────────────────────────────────────┐
      │  Postgres: AsyncPostgresSaver checkpoints │
      │            + ops records + slack_events   │
      └──────────────────────────────────────────┘
              │
              ▼  streamable-http
      ┌──────────────────────────────────────────┐
      │  MCP servers (separate processes)         │
      │  grafana:8005  logs:8006  monitor:8004    │
      │  cls:8003        ← sync I/O lives here    │
      └──────────────────────────────────────────┘
                       │
                       ▼  Milvus 19530 (RAG corpus)
```

Two processes rather than one is a direct consequence of §2.2. It also buys restart
independence: an `oncall-api` deploy does not drop the Socket Mode connection, and a
`slackd` crash does not take the web UI down.

**Socket Mode operational notes** (from the research pass):
- Slack recycles connections every few hours with a ~10s `refresh_requested` warning, and
  caps an app at 10 concurrent connections. Run **≥2 `slackd` replicas** so a refresh never
  drops events; dedupe (§8.2) makes duplicate delivery safe.
- Failing >95% of deliveries within 60 minutes **disables the app's event subscriptions**
  and needs a manual re-enable. A crash-looping container can take the bot offline, so
  handler-level exceptions are caught and reported into the thread, never allowed to escape.

---

## 5. Async discipline

### 5.1 Where synchronous code is allowed

| Code | Where it runs | Why it is safe |
|---|---|---|
| `httpx.Client` (Grafana, ES) | inside an MCP server process | blocking is local; the agent talks to it over async HTTP |
| `subprocess.run` (ripgrep, git) | `await asyncio.to_thread(...)` | explicit hand-off, bounded by tool timeout |
| `psycopg.connect` | replaced by `psycopg.AsyncConnection` | on the hot path; must not block |
| `resolve()`, `match_alert()` | in-process, called directly | pure computation, no I/O, microseconds |

### 5.2 Ordering inside a node

The old `pipeline.triage()` ran evidence collection sequentially. The baseline node fans out
with `asyncio.gather` over independent probes; the **alert's own query still resolves first**
and is written to `state["baseline"][0]` (§9, alert-own-query-first). Concurrency changes
wall-clock, never ordering of the record.

### 5.3 Enforced, not intended

`.importlinter` contracts, run in CI:
- `app.graph` and `app.evidence` must not import `httpx.Client`, `psycopg.connect`, or `subprocess`
- `app.domain` must not import `app.graph`, `app.tools`, or any LLM package
- `app.slack` and `app.api` must not import `app.graph.nodes` directly — only `build_graph`
- `app.evidence.baseline` must not import `app.slack` or anything carrying thread context

---

## 6. Routing — facts in code, intent in the model

**Revised during implementation.** The first version of this section routed purely on
structure: a thread containing an alert always got a full investigation, whatever the
engineer typed. That is too blunt, and it is wrong in a case that comes up constantly.

An alert is firing. The engineer asks *"what is server-feed?"* — a definitional question
they happened to type in this thread. Pure structural routing runs six metric queries and a
plan-execute loop to answer it. Slow, noisy, and not what was asked.

So there are two gates, and they answer different kinds of question.

### 6.1 Gate one: is there an alert here? (code)

A fact Slack already knows. Asking a model to re-derive it would be strictly worse.

`has_alert_context(event, thread) -> (bool, rule, why)`, short-circuiting:

| # | Test | Basis |
|---|---|---|
| 2 | thread root's `bot_id`/`app_id` is a configured alert sender | **provenance** |
| 3 | root is a bot message and the channel is a configured alert channel | provenance |
| 4 | a prior turn in this thread was triage | continuity |
| 5 | `match_alert(root.text)` hits | keyword table, last resort |
| 6 | otherwise | no alert |

Rules 2–3 are the important ones: they read Slack metadata, so **an alert nobody has
catalogued still gets the evidence floor**. Rule 5 exists only for an alert a human pasted
in by hand.

### 6.2 Gate two: is this message about diagnosing it? (model)

Only consulted when gate one said yes — a thread with no alert has no misroute to protect
against, so most turns never pay for this call.

`classify_intent(text) -> investigate | ask`

- **investigate** — including a narrow framing: *"is it the cold start thing again?"*,
  *"CPU looks fine to me"*. A narrow question is still a request to diagnose this alert, and
  §3.4 is explicit that the question shapes emphasis and never collection. Answering it by
  checking only cold start is the failure §3.3 exists to prevent.
- **ask** — the message is not about diagnosing this alert at all. *"What is server-feed"*,
  *"what does error code 43 mean"*.

Three things keep a semantic gate from becoming the §1.1 recognition gate:

1. **The misroutes are not symmetric, so the bias is written in code.** Answering a question
   with an investigation wastes a minute; answering an investigation with a definition means
   the engineer believes triage happened when it did not. Uncertainty, an unparseable
   verdict, an unreachable model, and an empty mention all resolve to `investigate`.
2. **A chat answer inside an alert thread is disclosed**, with an offer to upgrade: *"I read
   this as a question rather than a request to diagnose the alert in this thread, so I
   haven't investigated it. Say **investigate** and I will."* A misroute the engineer can
   correct in one word is a nuisance; a silent one is the thing worth designing against.
3. **The judgement lives outside the graph.** No node concludes that an investigation was
   unnecessary — `entry_for(state)` only reads a decision that was already made.

### 6.3 Two shapes of work, one entry

Slack has no navigation, so the *entry* must be unified. That does not mean execution has to
be, and forcing a knowledge question through plan-execute-replan costs four model calls
where two will do.

```
                    ┌── turn == "chat" ──► chat ──────────────────────► END
START ── entry_for ─┤                      (tool-calling, bounded rounds)
                    └── otherwise ───────► baseline ──► planner ──► executor
                                           (the floor)      ▲          │
                                                            └─ replanner ─► respond
```

Guarantees, each with a test in `tests/test_graph.py::TestWiring`:

- the planner is reachable **only** from `baseline`
- `chat` cannot reach the planner — it is a different shape of work, not a shortened
  investigation that quietly lost its evidence floor
- an unset `turn` enters at `baseline`, so the default is the safe direction

### 6.4 A mention in a channel is not a thread

`thread_ts` is present only on a reply. The common idiom
`thread_ts = event.get("thread_ts") or event["ts"]` is right for *addressing* a reply, but it
makes a top-level mention look like the root of a thread containing only itself — which
hides exactly the distinction routing depends on. `in_thread(event)` reports it explicitly.

**The agent reads one thread, never a channel.** `conversations.replies` is thread-scoped and
bounded at `slack_thread_limit`; `conversations.history` is not used on the default path. A
busy incident channel may hold five unrelated alerts, and pulling them in would dilute the
evidence and manufacture ambiguity about which one was being asked about.

Two real cases survive that rule:

- **An alert pasted into the mention itself** routes to triage via rule 5. No history needed.
- **A top-level mention that seems to reference a recent alert**, in a configured alert
  channel, produces an *offer* — "this channel had a `news-list-for-channel` alert 5 minutes
  ago; is that the one?" — never an assumption. Bounded to recent bot messages, in alert
  channels only, and only as a suggestion.

### 6.5 Side effects need an affordance, not a word

`_wants_writeback()` substring matching (`"record this"`, `"resolved"`) is **deleted**. A
sentence containing the word "resolved" must not be able to open a pull request. Write-up and
rating arrive through Block Kit affordances carrying an `invocation_id`.

## 7. The tool layer

### 7.1 Local versus MCP

| Tool | Kind | Why |
|---|---|---|
| `retrieve_knowledge` | local | Milvus client is async; RAG over `aiops-docs` + `rec-knowledge` |
| `search_code`, `read_file`, `git_log`, `list_dir` | local | ripgrep + path containment must stay in our process (§9) |
| `resolve_deployment`, `blast_radius` | local | pure computation over `config/deployments.yaml` |
| `quantify_impact` | local | **the source is baked in and there is no `source` parameter** |
| `query_metric`, `fetch_alert_rule` | MCP (grafana) | owns the sync httpx client |
| `search_logs` | MCP (logs) | owns ES/Loki; returns a provenance envelope |
| `query_cpu_metrics`, `search_log` (cls) | MCP (ported) | synthetic — see §7.2 |

### 7.2 The provenance envelope, enforced at load

This is the highest-risk correctness issue in the system (tech-design §5.1) and it gets
harder in the new architecture, not easier: **MCP has no provenance field**, and
`MultiServerMCPClient.get_tools()` returns plain LangChain tools with a name, a description
and a schema. A model asked "how many users were affected" will happily call a log-search
tool whose `total` is a `limit=100`-capped row count and cite it as an impact number.

So provenance is attached **where tools are loaded**, not where they are written:

```python
# app/tools/registry.py
async def load_tools() -> list[BaseTool]:
    tools = local_tools() + await mcp_client.get_tools()
    return [wrap_with_contract(t, SOURCES.get(t.name, UNKNOWN)) for t in tools]
```

`config/sources.yaml` declares, per tool name: `sampling_rate`, `retention`, `usable_for`,
`not_usable_for`, and `synthetic: true|false`. The wrapper post-processes every result into
an `Observation` whose `render()` prints the caveat **in the same string as the number**, so
it cannot be read without it.

Three rules, in code:

1. **Default deny.** A tool absent from `sources.yaml` gets `usable_for: [qualitative]` and
   `not_usable_for: [impact_quantification]`. Adding an MCP server cannot silently add an
   impact source.
2. **Impact is not a tool the model routes.** `quantify_impact()` is local, takes `host` and
   a window, and has no parameter that could select a source. Counting from a sampled log is
   not prohibited — it is *inexpressible*.
3. **Synthetic data is disclosed structurally.** Any observation from a tool marked
   `synthetic: true` sets a state flag; the renderer emits the fixture banner and the
   responder's system instruction is amended. The ported `monitor_server.py` and
   `cls_server.py` generate data with `random.uniform()` — they are marked synthetic on day
   one, and the flag is what keeps that honest.

### 7.3 Two retrieval mechanisms, on purpose

Adopting Milvus does not delete ripgrep. They answer different questions:

- **Vector search** over incident write-ups and runbooks — "which past incident resembles
  this", a paraphrase-tolerant judgment.
- **Lexical search** over code — `ERR_4021`, `x_status_code`, a pod pattern. Identifiers are
  exact tokens, and embedding them loses the property that makes them findable.

Both are tools; the planner picks. What is *not* negotiable is that the code tool stays
lexical, with the one-hit-per-file flood guard and the path-traversal containment (§9).

---

## 8. Slack

### 8.1 Ack, then work

The 3-second deadline applies to Socket Mode exactly as it does to the Events API — the
envelope must be acked over the WebSocket or Slack retries. So every handler is:

```python
@app.event("app_mention")
async def on_mention(event, ack, client, logger):
    await ack()                                   # ≤3s, always
    asyncio.create_task(run_turn(event, client))  # 60–120s, off the socket
```

Nothing that touches Grafana, Milvus, or an LLM runs before `ack()`.

### 8.2 Idempotency

Slack retries unacked envelopes and redelivers on reconnect; with ≥2 `slackd` replicas
(§4) duplicate delivery is expected, not exceptional. Without a key, one @-mention becomes
two investigations and two replies.

`slack_events(event_id text primary key, claimed_at timestamptz)`. The claim is an
`INSERT ... ON CONFLICT DO NOTHING`; losing the race means another replica owns the turn.

The claim happens **after** the turn is durably recorded, not before. The reverse ordering —
claim, then enqueue to an in-memory queue — is what makes a restart drop the turn
permanently: the dedupe row survives and blocks the redelivery that would have retried it.

### 8.3 Progress

An incident channel watching a silent bot for ninety seconds assumes it has hung. But
`chat.update` is Tier 3 (50+/min per method, and Slack asks for ~1 req/sec per channel), and
the old `slack_app.py` called it once per investigation step with no batching.

`app/slack/progress.py` is a batched writer:
- coalesces updates and emits at most **one edit per 1.5s**
- always emits the first update immediately (the engineer needs to see it started) and the
  last one unconditionally
- honours `Retry-After` on 429 and degrades to a longer interval rather than dropping

`chat.startStream` / `appendStream` (Tier 4, 100+/min) is the better path and is implemented
behind `SLACK__USE_STREAMING`, defaulting **off**: the API reference says `thread_ts` is
"only supported in channels where the whole channel is one session... and returns
`invalid_thread_ts` elsewhere", while the SDK's own examples pass `thread_ts`. Those
contradict, and an on-call bot lives in threads. The adapter tries the stream and falls back
to `chat.update` on `invalid_thread_ts`, so enabling the flag is safe to test in a real
workspace.

Progress content comes from LangGraph's `astream(stream_mode=["updates", "custom"])` — the
same event stream the SSE endpoint consumes. Nodes emit fine-grained progress via
`get_stream_writer()`.

### 8.4 Reading the thread

`conversations.replies` (scopes: `channels:history`, `groups:history`, `im:history`,
`mpim:history`). Message classification, which routing (§6) depends on:

| Sender | Shape |
|---|---|
| Incoming-webhook alert | `subtype="bot_message"`, has `bot_id`, has `username`, **no `user`** |
| Bot-token alert | has `bot_id` + `bot_profile` + `app_id`, no `bot_message` subtype |
| Human | has `user`, no `bot_id` |
| Us | `bot_id` matches our own |

**`bot_id` and `bot_user_id` are different identifiers** (`B…` vs `U…`). The widely-copied
LangChain reference implementation compares one against the other, so its "stop at my last
message" loop is dead code. Both are resolved once at startup from `auth.test` and stored
explicitly.

### 8.5 Conversation

Two surfaces:
- **`app_mention`** in a channel — the summoned path. Unchanged in spirit from §12: the
  mention means a human read the alert and wants help.
- **`message.im`** — a DM with the bot is an ordinary conversation, no mention needed.

Both carry `thread_ts`; the LangGraph `thread_id` is
`slack:{team_id}:{channel_id}:{thread_ts}`, so the checkpointer *is* the thread memory that
§3.7 built by hand. `memory.summarize()`'s two ideas survive as state reducers: carry the
previous conclusion **and** what was already searched, bounded.

**Concurrency within one thread.** Two mentions in the same thread map to the same
`thread_id` and would interleave writes into one checkpoint. A Postgres advisory lock on
`hash(thread_id)` serializes turns per thread; a turn that cannot take the lock posts
"still working on the previous question" rather than corrupting state.

Slack mrkdwn is not Markdown. `app/slack/mrkdwn.py` rewrites `**bold**` → `*bold*`,
`[text](url)` → `<url|text>`, and `-`/`*` bullets → `•`. (The streaming API's `markdown_text`
accepts real Markdown — another reason to want §8.3's flag on eventually.)

### 8.6 Status, and what not to build

`agents.sessions.setStatus` replaces `assistant.threads.setStatus`; the `assistant_view`
surface is deprecated (EOL February 2027) in favour of `agent_view`. New work targets
`agents.sessions.*`. The status indicator **times out after two minutes** — uncomfortably
close to a 120s budget — so it is re-issued periodically during a long turn.

Scopes: `app_mentions:read`, `channels:history`, `groups:history`, `im:history`,
`mpim:history`, `chat:write`, `chat:write.public`, `commands`. Not `assistant:write` — since
2026-03-05 `chat:write` is accepted and `assistant:write` is being removed.

---

## 9. Constraint → mechanism → test

The catalogue of what must survive, and *what makes it survive*. A constraint whose
mechanism is "the prompt says so" is not on this list.

| # | Constraint | Mechanism | Test |
|---|---|---|---|
| 1 | Impact counts come from ingress, never sampled logs | `quantify_impact()` has no `source` param; §7.2 rule 2 | `test_impact_has_no_source_parameter` (signature assertion) |
| 2 | Every metric/log result carries sampling + retention | `wrap_with_contract` at tool load; default-deny | `test_unregistered_tool_defaults_to_qualitative` |
| 3 | The alert's own query runs first | `baseline` node writes it to `baseline[0]` before any fan-out | `test_alert_query_is_observation_zero` |
| 4 | Empty ≠ healthy | `Observation.render()` emits the literal caveat | `test_empty_series_renders_not_healthy` |
| 5 | Timestamp metrics render as age | `is_timestamp_metric` → `_relative()` | `test_pod_start_time_renders_as_age` |
| 6 | Resolution says how sure it is | `Resolution{confidence, matched_by, note}` | `test_unknown_host_is_unresolved` (already passing) |
| 7 | Baseline cannot see the question or priors | `collect_baseline(identity, rule, resolution, *, minutes)` — **the signature is the enforcement** | `test_baseline_signature_is_frozen` (inspect.signature) |
| 8 | Investigation is unconditional | routing is structural (§6); `baseline` is on the only edge into `planner` | `test_unknown_alert_still_produces_observations` |
| 9 | Thread priors never suppress collection | priors enter `state["priors"]`, which `baseline` does not read (see 7) | covered by 7 |
| 10 | Synthetic data is disclosed | `synthetic` flag → renderer banner + system instruction | `test_synthetic_tool_sets_banner` |
| 11 | Loop terminates | `guards.budget`: wall-clock **and** step count, replan cannot grow the plan | `test_budget_stops_on_wall_clock` |
| 12 | Partial findings survive a failure | `respond` node summarizes `past_steps` and refuses to invent a cause | `test_partial_findings_no_invented_cause` |
| 13 | Diagnosis cites evidence and calls victim/cause | structured output schema, both fields required | `test_diagnosis_schema_requires_victim_or_cause` |
| 14 | Model unavailable is an error | no silent fallback; degraded tier is labelled in the reply | `test_degraded_tier_is_disclosed` |
| 15 | Path traversal contained | resolve-and-check under repo root in `code_search` | `test_path_escape_rejected` |
| 16 | Runs are recorded, never retrieved | `app/storage` has no read path reachable from `app/graph` | import-linter contract |
| 17 | Unreviewed ≠ correct | `verdict IS NULL` excluded from rates | `test_unreviewed_excluded_from_stats` |
| 18 | One @-mention = one investigation | `slack_events` dedupe (§8.2) | `test_duplicate_event_id_is_dropped` |
| 20 | Uncertain intent investigates | classifier failure / bare mention / bad verdict all return `investigate` in code | `test_an_unavailable_model_investigates`, `test_a_bare_mention_investigates_without_a_model` |
| 21 | A chat answer in an alert thread is disclosed | `Decision.offer_triage` → the reply appends the offer | `test_that_chat_answer_discloses_and_offers_to_triage` |
| 22 | Chat is not a shortened investigation | `chat` node has no edge to the planner | `test_chat_cannot_reach_the_planner` |
| 23 | A narrow question does not narrow collection | intent returns `investigate` for hypothesis-shaped questions | `test_a_narrow_hypothesis_is_still_an_investigation` |
| 24 | Intent is not consulted without an alert | gate two runs only when gate one passed | `test_intent_is_not_consulted_without_an_alert` |
| 25 | A channel is never read as if it were a thread | `conversations.replies` only on the default path | `test_a_top_level_mention_has_no_alert_context` |
| 19 | Banners are computed once | only `app/render/answer.py` emits them; adapters call it | import-linter: no adapter builds banner strings |

Item 19 deserves its own note. In the old repo every banner — sample data, unconfirmed
attribution, unresolved deployment, degraded model, and the four-number metric accounting —
is computed in `pipeline.format_reply()`. Split that across a Slack renderer and an SSE
renderer and they drift; the one that drifts is the one that silently stops disclosing.
There is exactly one renderer, and the adapters differ only in markup.

---

## 10. State

```python
class OncallState(TypedDict):
    # turn input
    input: str                                    # what the engineer typed
    turn: str                                     # triage | followup | chat | writeup | rating
    conversation_id: str                          # slack:{team}:{channel}:{thread_ts}

    # deterministic floor — written only by the baseline node
    identity: AlertIdentity | None
    rule: AlertRule | None
    resolution: Resolution | None
    baseline: Annotated[list[Observation], operator.add]

    # thread context — read by planner/responder, NOT by baseline
    priors: list[str]
    thread_digest: str

    # plan-execute-replan
    plan: list[str]
    past_steps: Annotated[list[ExecutedStep], operator.add]

    # disclosure
    used_synthetic: bool
    degraded_model: str | None
    skipped: Annotated[list[SkippedProbe], operator.add]

    response: str
```

`baseline` and `past_steps` are `operator.add` channels: append-only, so no later node can
shrink the evidence floor. `priors` is a plain overwrite channel and is deliberately *not*
an argument to anything in `app/evidence/`.

`skipped` is how "we did not measure this, and here is why" reaches the reply. A probe with
no input to run on produces a `SkippedProbe`, never silence — silence is what makes a thin
pack read like a complete one.

---

## 11. Migration order

Each step is independently landable and leaves the repo working.

| # | Step | Verifiable by |
|---|---|---|
| 1 | Scaffold: `app/` skeleton, `config.py`, pyproject with 3 entry points, docker-compose, import-linter | `make lint` passes; `oncall-api` serves `/health` |
| 2 | `app/domain/` — port `Resolution`/`resolve`, `match_alert`, repo registry, `SourceContract`; YAML configs | old `TestResolutionConfidence` tests ported and green |
| 3 | `app/evidence/` — `Observation` envelope, `identify`, `rules` ladder, `collect_baseline`, `quantify_impact`, accounting | constraints 1–7 in §9 green |
| 4 | `app/tools/registry.py` — local tools + MCP merge + `wrap_with_contract` | constraints 2, 10 green |
| 5 | `app/graph/` — state, nodes, guards, `build_graph()`; MemorySaver first | constraints 8, 11–13 green; CLI `oncall triage` runs end to end |
| 6 | `app/render/answer.py` + `oncall-api` SSE | constraint 19; browser shows a full pack |
| 7 | `mcp_servers/grafana_server.py` — the sync httpx client moves here | §2.1 resolved; import-linter contract added |
| 8 | `app/slack/` — handlers, router, thread reader, dedupe, mrkdwn | constraint 18; a real mention produces a reply |
| 9 | `progress.py` batched writer + `get_stream_writer()` in nodes | a 90s run shows visible progress under rate limits |
| 10 | `AsyncPostgresSaver` + advisory lock + ops records (async psycopg) | restart mid-turn resumes; concurrent mentions serialize |
| 11 | Milvus RAG ingestion of `rec-knowledge` + `aiops-docs` | `retrieve_knowledge` returns real hits |
| 12 | Write-up + rating affordances (Block Kit), `agents.sessions` status | constraint 17; PR opens from a thread |
| 13 | Delete `src/oncall_agent/` | full suite green without it |

---

## 12. Open risks

Stated rather than resolved, because each needs a real workspace or cluster to settle.

1. **`chat.startStream` with `thread_ts`** — the docs and the SDK examples contradict (§8.3).
   Mitigated by defaulting to `chat.update` and falling back on `invalid_thread_ts`, but the
   better path stays unproven until tested.
2. **Model choice.** The sibling repo is DashScope/Qwen; this repo's judgment prompts were
   tuned on Gemini. `app/core/llm_factory.py` keeps it configurable, but the diagnosis
   quality bar in §9 constraint 13 must be re-checked after the switch, not assumed.
3. **Ported MCP servers are entirely synthetic.** `monitor_server.py` and `cls_server.py`
   generate ramps with `random.uniform()`. They are flagged (§7.2 rule 3) so nothing presents
   them as measurements, but the system has no real metric backend until step 7 lands and a
   Grafana URL is configured.
4. **Socket Mode at ≥2 replicas** relies on the dedupe table being correct under a race.
   Worth a deliberate duplicate-delivery test, not just the unit test in §9 item 18.
5. **The old repo's `google-genai` retry semantics** (`_TRANSIENT` string matching, tier
   fallback with disclosure) do not port directly to another provider's exception types.
   Constraint 14 needs a provider-specific implementation, not a copy.
