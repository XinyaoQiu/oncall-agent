# On-Call Agent Skeleton — Design Spec

**Date:** 2026-08-21
**Scope:** Runnable Python skeleton implementing all four rollout stages of
`docs/tech-design.md`, with external systems behind interfaces and fake implementations.

The tech design was amended alongside this spec (§2). This document covers the skeleton;
the amendments are already merged into `docs/tech-design.md`.

**Amended 2026-08-22 (§2A, §2B).** §2A: the skeleton had made alert recognition a
precondition for investigating at all. §2B: nothing carried a lesson from one run to the
next — the feedback loop (per-alert entries, thread clock, summary and rating) closes it,
and reverses §7.1's severity admission gate.

§2A detail follows; §2B is below it.

**§2A:** review found the skeleton had made alert recognition a
precondition for investigating at all. §2A records that defect, the corrections merged
into the tech design (§1.1, §3 Stage 0, §3.1 ladder, §3.5, §3.6, §4.1, §6.2), and the
implementation order for the next pass. §3–§7 below describe the original skeleton and are
superseded where §2A conflicts; §2A.6 reconciles the directory layout with what was built.

---

## 1. Goal and non-goals

**Goal:** a skeleton that runs end-to-end against fake data sources, with real structure
for every component the tech design names. Running the CLI against a fake alert produces a
complete evidence pack.

**Non-goals for this pass:**

- Connecting to real Grafana / Mimir / Elasticsearch / K8s / Slack
- Semantic code search (tech design §4.2 puts it out of scope for v1)
- Slack app manifest, OAuth, event subscription setup
- Production deployment concerns

---

## 2. Tech design amendments (already applied)

Six changes, driven by review of the original draft.

### 2.1 §3 — alert preprocessing is two-stage

Alerts arrive as **rendered Slack messages**, not structured payloads. Stage 1 identifies
*which alert this is* from the text (rules → LLM fallback, approximate is fine). Stage 2
looks up the authoritative rule in Grafana/Alertmanager and reproduces it.

**The Slack message is an identification signal, not a data source.** Numbers in the text
never enter the evidence pack. The asymmetry: a wrong identification fails loudly at rule
lookup; a wrong number fails silently and reaches an incident review as fact.

### 2.2 §3.3 (new) — thread context as prior input

Thread discussion before the @-mention is used to reorder work and to avoid repeating what
people just said. **Hard constraint: it never suppresses evidence collection.** A human
saying "CPU is fine" does not skip CPU collection — they may have looked at the wrong
dashboard, or looked before the spike.

### 2.3 §12 — entry point is a Slack thread @-mention

Replaces slash-command-then-auto-push. Automatic posting on every alert is deliberately
off the roadmap: unsolicited posting trains people to scroll past the agent.

### 2.4 §5.2 (new) — operational records in Postgres

The design covered knowledge (git) and its index, but omitted per-invocation records:
which alert, which queries, what the pack contained, what hypothesis at what confidence,
whether a human corrected it. Plus the labeled set and eval results.

**Mandatory, not instrumentation:** every §9 metric is computable only from these records.

**Postgres, not SQLite.** The agent runs as multiple replicas, so the store must be shared
and network-reachable — a single-file database is structurally incompatible, not merely
slower. The §9 metrics are time-bucketed aggregations; packs land in `jsonb`.

### 2.5 §6.1 — knowledge retrieval uses grep, not embeddings

**The vector index is removed.** `rec-knowledge` is retrieved with the §4.2 toolset —
ripgrep over a local git repo, driven by the same agentic loop — pointed at the knowledge
repo instead of the service repo.

Rationale: a few hundred markdown files queried with the exact identifiers §3 already
extracts is lexical retrieval's strong case. The synonymy concern that motivated
embeddings ("OOM" vs "memory exhaustion") is better handled by the loop — the model sees
round one's hits and re-queries with different wording — which is the same argument §4.2
already makes for code. One less component, no new infrastructure.

**Retrieval lives on the hypothesis side.** "Which past incident resembles this one" is a
judgment, not a measurement.

### 2.6 §8.1 — an unavailable model is an error, not a degraded mode

The original design treated "fall back to the evidence pack" as a feature. It isn't: a pack
produced without the model is missing victim/cause discrimination, incident history, and
code localization, but renders identically to a complete one. In an incident nobody stops
to ask whether half the analysis is silently absent.

Failure reporting is two-layered:

```
A call fails
  ├─ LLM still reachable → the model composes the failure report itself
  └─ LLM unreachable     → deterministic error: failed step + what was collected
```

**The failure report is schema-constrained** to `collected` / `failed_step` / `reason` /
`suggested_next_action`. No field accepts a root-cause guess — otherwise a model asked to
explain a failure reaches for "based on available evidence, this is probably…", which is
the exact output the section exists to prevent.

---

## 2A. Amendments — 2026-08-22: investigation is unconditional

A second review found that the skeleton had made **alert recognition a precondition for
investigating**, which inverts the product's value. These amendments are applied to
`docs/tech-design.md` and define the next implementation pass.

### 2A.1 The defect, as measured

Running the built skeleton against an alert absent from `KNOWN_ALERTS`:

```
$ oncall-agent evidence --sample "[FIRING] PaymentGatewayTimeout app=billing-svc path=/api/v2/charge"
alert:      unknown (via rules)
labels:     {'app': 'billing-svc', 'path': '/api/v2/charge'}
rule:       - not found -
deployment: - unresolved -
                      ← 0 metric queries. Nothing else printed.
```

`evidence.gather()` is three `if` blocks — on `rule`, on `deployment`, and on a host for
impact. An unrecognized alert fails all three and the function returns an empty list. The
benign checklist returns `[]` for the same reason. The investigation loop still runs, so
the model can grep, but playbook steps 1, 2, 3, 5 and 7 produce nothing.

This is the population where the agent is worth the most. A recognized alert is one an
engineer already knows how to handle.

### 2A.2 What changes (tech design §1.1, §3.5)

**Investigation is unconditional.** Recognition, precedent, and deployment resolution
adjust confidence, ordering, and precision. None of them gates whether monitoring, logs,
dashboards or code are consulted.

Concretely, `gather()` stops being a conjunction of preconditions and becomes an
unconditional baseline plus optional refinements:

| Before | After |
|---|---|
| `if rule:` query it, else nothing | §3.1 four-rung ladder; rung 4 synthesizes from labels, labelled as such |
| `if deployment:` pod/replica, else nothing | Query by resolved name when available, by raw `app` label when not |
| `if host:` impact, else nothing | Unchanged when truly no host — but the skip is *reported*, not silent |
| `if known_alert:` checklist, else `[]` | Unchanged; it is additive by definition |
| — | **New:** golden-signal probes driven by any label present |

**An empty evidence pack becomes a defect.** When every probe genuinely has no input, the
reply is a question ("give me a service name or a dashboard link"), never a diagnosis.

### 2A.3 Alert links become the monitoring entry point (tech design §3.6)

Current `extract_labels()` filters to a seven-key whitelist and discards everything else,
including every URL. Thread text reaches the agent whole, so the links are already in hand
— they are simply thrown away. `KnownAlert.grafana_rule_uid` is declared and never read,
the same gap showing up as a dead field.

**Following a link is not a new capability.** `GrafanaClient` already authenticates with a
bearer token and issues API requests; a link just names a target.

**Revised from an earlier draft of this amendment.** That draft said code would translate
each link into an API call, and dismissed link-following as "parsing, not fetching". Both
were wrong. The distinction is not parse-versus-fetch — the agent fetches, over APIs rather
than HTML — and translation in code means one adapter per system: Grafana dashboards
(two hops plus `$var` substitution), Grafana Explore (JSON in a query parameter), Prometheus
(`15m` durations), Loki, Kibana (rison encoding), raw ES. Six adapters, each broken by an
upstream URL-schema change, before counting internal short links and SSO redirects. The
failure mode is the bad one: a stale adapter builds a malformed query, the backend returns
empty, and §5.2 makes empty indistinguishable from healthy.

The split that survives (tech design §3.6):

| Part | Owner | Rationale |
|---|---|---|
| Which system | Code — host matched against configured datasources | Doubles as the allowlist |
| Time window | Code | The one silent failure: ms read as s shifts the window by decades and returns empty |
| The query | **Model** | Six evolving query languages; enumeration is a losing race |
| Execution | Code — one client per datasource type | Auth, timeouts, §5.1 provenance |

**A tool is added to the loop:**

```
open_link(url) → { system, time_window, raw_params, hint }
```

`system: "unknown"` still returns `raw_params`, so an unfamiliar link degrades to
"the model reads the URL itself" rather than to nothing. Reach is bounded by configured
hosts — never inferred from URL path shape (§10.4).

This revises the earlier "no new tools" position. The tool set stays *fixed at session
start* per §10.4; that was always about mid-conversation expansion, not about the set's
contents. `query_logs` and the §5.1 provenance wrapper remain the other additions.


### 2A.4 Deployment resolution stops guessing silently

`resolve_deployment(path="/api/v2/charge")` currently returns `server-default`, because
the catch-all entry's `/` prefix matches everything. Downstream pod and replica queries
then succeed against a workload that does not serve the alerting path — true data, wrong
subject, rendered exactly like a correct result.

Resolution gains an explicit confidence: a catch-all match or a host-match-without-path is
returned flagged, and the flag reaches both the pack and the prompt. Unresolved is a valid
state that drops identity-dependent probes and keeps label-driven ones.

### 2A.5 Precedent short-circuit (tech design §6.2)

A close `rec-knowledge` match is surfaced **immediately, with attribution and date**,
while collection continues. Match detection is deterministic — alert name, label overlap,
metric shape — never a model judgment, since a model asked "is this the same" leans
toward yes. The engineer decides whether the precedent settles it; the agent supplies the
comparison and the evidence that would contradict it.

The diagnosis prompt's existing rule ("a runbook tells you what to check, not what
happened") is unchanged and still governs the model. §6.2 is a code-emitted attribution
alongside the analysis, not an input to it.

### 2A.6 Directory layout: spec reconciled to the built code

§3 below describes `ingest/`, `evidence/`, `hypothesis/`, `sources/`, `llm/`. The
implementation settled on a flatter layout, which is what exists and what these amendments
target:

| Spec (§3) | Built | Notes |
|---|---|---|
| `ingest/identify.py`, `ingest/thread_context.py` | `analysis/identify.py`, `analysis/thread.py` | `ingest/slack.py` → top-level `slack_app.py` |
| `evidence/steps.py`, `benign.py`, `assembler.py` | `analysis/evidence.py` | Single module; still model-free |
| `evidence/deployment.py` | `config.py` | Shard table and resolver together |
| `hypothesis/generator.py` | `analysis/diagnose.py` | |
| `hypothesis/search_loop.py` | `investigate/loop.py` + `investigate/tools.py` | Multi-repo via `repos.py` |
| `writeback/*` | `analysis/writeback.py` + `sources/knowledge.py` | PR path is real, not interface-only |
| `llm/base.py`, `gemini.py`, `fake.py` | `llm.py` | Fake LLM lives in tests |
| `sources/elasticsearch.py`, `k8s.py` | **not built** | §5.1 provenance wrapper still outstanding |

The `evidence/` import invariant (§3.1.2) survives as a property of `analysis/evidence.py`,
which imports nothing from `llm.py` — still worth an enforcing test.

### 2A.7 Implementation order

1. **Stage 0 extractor** — labels without a whitelist, URLs, identifiers, timestamps.
   Pure function, fully testable, no model, no network.
2. **`open_link` (§3.6)** — host→datasource matching against configured URLs, time-window
   normalization across dialects, `raw_params` passthrough. Registered as a loop tool.
   Yields the real firing window in place of `fired_at=datetime.now()`.
3. **`gather()` de-conditioned (§3.5)** + the §3.1 ladder with provenance labels.
4. **Resolution confidence (§4.1)** — flag catch-all and host-only matches.
5. **Reporting** — `format_reply` states what was skipped and why; the pack distinguishes
   authoritative from synthesized queries.
6. **Precedent short-circuit (§6.2)** — deterministic matcher, attributed output.
7. **`query_logs` tool** and the §5.1 provenance wrapper — the largest remaining gap, and
   what makes `open_link` on a Loki or Kibana link actionable rather than informational.

Steps 1–5 are the correctness fix and should land together; 6 and 7 are additive.

### 2A.8 Tests this pass must add

- **Cold-start coverage** — an alert matching no registry entry, against an empty
  knowledge repo, must still produce metric queries. Asserts non-empty, which is the
  §1.1 regression guard.
- **Empty pack is impossible with any label present** — given only `app=x`, at least one
  probe runs.
- **`open_link` window extraction** — epoch-ms, `now-15m`, and `range_input=15m` all
  normalize to the same interval. A millisecond value must never be read as seconds: assert
  the window is hours wide, not decades.
- **`open_link` host allowlist** — a configured host resolves to its datasource type; an
  unconfigured host is reported as external and **issues no request** (assert no outbound
  call). Path shape never grants reach: a `/d/`-style path on an unconfigured host stays
  external.
- **Unknown-system degradation** — an unrecognized but allowlisted link still returns
  `raw_params` and a window, never an error.
- **Provenance labelling** — a synthesized query is never presented as the alert's own.
- **Resolution flagging** — `path="/api/v2/charge"` returns `server-default` *flagged
  low-confidence*, never as a plain match.
- **Precedent short-circuit** — fires only on the deterministic condition; output carries
  entry name and date; collection still completes.
- **Skip reporting** — a probe skipped for missing input appears in the output with its
  reason.

---

## 2B. Amendments — 2026-08-22: the feedback loop

§2A made investigation unconditional. This set makes it *improve over time*: a correction
an engineer gives in a thread should change what the agent collects on the next firing of
that alert, without anyone re-typing it.

### 2B.1 What was missing

Nothing carried a lesson from one run to the next. `invocations` and `investigation_steps`
record every run in full, but `pipeline.triage()` never reads them — `recent/replay/stats`
are CLI-only. `rec-knowledge` is retrieved, but writing to it required an explicit
`record this` and passed a P0/P1 admission gate. An ordinary alert where someone said
"check XX" left no trace that would change the next run.

### 2B.2 Learning rides the knowledge base — no new store

The mechanism does not need a rules table. A `rec-knowledge` entry that records *what was
checked and what it revealed* is already retrieved on the next firing; the baseline probe
(§3.5) reads it and queries accordingly. What was missing was that entries did not carry a
"what to check" section, and that per-occurrence filenames made accumulation impossible.

An earlier draft of this amendment proposed a separate `learned_probes` table in Postgres.
Dropped: it duplicates retrieval that already exists, and splits "what we know about this
alert" across two stores with different trust levels.

### 2B.3 Per-alert files, rewritten in place (§7.2)

`writeback.py` currently emits `incidents/{date}-{slug}.md` — a new file per occurrence, so
`UPDATE`/`supersedes` can essentially never match. Changes to:

- **Filename keyed by alert:** `alerts/<alert-name>.md`. Present → UPDATE, absent → ADD.
- **Full rewrite, not append.** The file stays one coherent account rather than a changelog.
- **Deletions stated in the PR description.** A rewrite that drops a correct finding renders
  like any other edit; prose calling out what was removed is what a glance-level review can
  actually catch.
- **A "what to check" section**, which is what §3.5 consumes on later runs.

### 2B.4 Admission moves to merge (§7.1)

The P0/P1 gate is removed. Every completed triage produces a candidate entry and a PR;
merging is the gate. Severity does not predict whether a run taught something — the
valuable cases are ordinary alerts where an engineer supplied what the agent lacked, which
is exactly what a severity gate discards.

Unmerged candidates are not rejected knowledge; they remain run records in Postgres, which
is what they always were.

### 2B.5 The clock (§7.4) — new architecture

`slack_app.py` is purely event-driven. It gains a **per-thread timer**: set on the first
mention, reset on every subsequent message in that thread, fires the summary after N hours
of silence (default 4). An explicit "resolved" fires it early.

In-memory, so restarts drop pending timers — acceptable, one missed summary. No polling.

The summary posts what the run concluded, **what it learned in the engineer's own terms**,
a link to the PR, and rating controls.

### 2B.6 In-thread guidance is thread-scoped

A mid-thread "check XX" changes collection for that run only. It is written nowhere at the
moment it is said. Durable change happens through the summary's PR, reviewed by a human —
a path that already assumes untrusted input (§10.4). This keeps a sentence in Slack from
becoming persistent agent behavior.

### 2B.7 Scoring measures; it does not gate

Every run produces its candidate whether or not anyone rates it. Ratings annotate the
Postgres record for §9. Gating the write on a score would make the learning rate track
engineer diligence, and would break the §9 requirement that unrated runs count as unrated.

Rating controls move into the thread summary; the CLI `verdict` command stays for
backfill but is not the collection path.

### 2B.8 Implementation order

Follows §2A.7's steps 1–7, then:

8. **`writeback` keyed per alert** — filename, UPDATE-in-place, "what to check" section,
   deletion disclosure in the PR body. Independently testable, no scheduler needed.
9. **§7.1 gate removal** — every triage produces a candidate.
10. **Thread scheduler** — per-thread timers, summary composition, posting.
11. **Rating controls** — Slack interactive components → `verdict` on the run record.
    Requires the Slack app to handle interaction payloads, not just events.
12. **§3.5 consumes "what to check"** — closes the loop; the collection layer reads the
    merged entry. Worth landing last, since it is the step whose effect is only visible
    once entries exist.

### 2B.9 Tests this pass must add

- **Filename keying** — two runs on the same alert target the same path; the second is an
  UPDATE, not a second file.
- **Deletion disclosure** — a rewrite dropping an existing line produces a PR body naming
  what was removed; an addition-only rewrite does not.
- **Thread scoping** — guidance in thread A does not appear in a subsequent run in thread B.
- **Timer reset** — a new message pushes the deadline out; silence fires it once, not
  repeatedly.
- **Score independence** — a run with no rating still produces its candidate entry.
- **Loop closure** — given a merged entry with a "what to check" item, the next run on that
  alert issues the corresponding query. This is the amendment's actual claim, and the only
  test that verifies it end to end.

---

## 3. Architecture

```
oncall-agent/
├── pyproject.toml
├── docker-compose.yml               # Postgres
├── src/oncall_agent/
│   ├── models.py                    # Alert, Evidence, EvidencePack, Hypothesis, FailureReport
│   ├── config.py                    # deployment shard table, known-alert registry, settings
│   │
│   ├── ingest/
│   │   ├── slack.py                 # thread data structures, @-mention parsing
│   │   ├── identify.py              # alert identification: rules → LLM fallback
│   │   └── thread_context.py        # prior extraction (§2.2)
│   │
│   ├── sources/                     # external systems: Protocol + fakes
│   │   ├── base.py
│   │   ├── grafana.py               # rule lookup + PromQL query
│   │   ├── elasticsearch.py         # provenance-wrapped log queries
│   │   ├── k8s.py                   # pod / HPA / deploy events
│   │   ├── repos.py                 # ripgrep + git tooling, shared by code and knowledge
│   │   └── fakes.py                 # fixtures for 4 real alert scenarios
│   │
│   ├── evidence/                    # deterministic — imports nothing from llm/ or hypothesis/
│   │   ├── steps.py                 # playbook steps 1, 2, 5, 7
│   │   ├── benign.py                # §3.2 benign-pattern checklist
│   │   ├── deployment.py            # §4.1 resolve_deployment / blast_radius
│   │   └── assembler.py             # budget-bounded assembly
│   │
│   ├── hypothesis/                  # Stage 3, disabled by default
│   │   ├── generator.py             # confidence-tagged synthesis
│   │   ├── search_loop.py           # §4.2 agentic loop, over code and rec-knowledge alike
│   │   └── failure.py               # §2.6 model-composed failure report
│   │
│   ├── writeback/                   # Stage 4
│   │   ├── admission.py             # §7.1 admission gate
│   │   ├── extractor.py             # extract + ADD/UPDATE/CONFLICT
│   │   └── pr.py                    # interface only; no git/GitHub calls in the skeleton
│   │
│   ├── llm/
│   │   ├── base.py                  # Protocol
│   │   ├── gemini.py                # Flash + Pro tiers
│   │   └── fake.py                  # deterministic, for tests
│   │
│   ├── storage/
│   │   ├── schema.sql
│   │   └── records.py               # invocation records, labeled set, eval results
│   │
│   └── cli.py
└── tests/
```

### 3.1 Three structural invariants

1. **`sources/elasticsearch.py` always returns provenance-wrapped results.** A `server_logs`
   result carries `sampling_rate` and `NOT_usable_for`; impact quantification is hard-routed
   to `lb_access_logs`. The model never sees a bare number. (tech design §5.1)

2. **`evidence/` imports nothing from `llm/` or `hypothesis/`.** The reason is **separation
   of concerns and independent testability** — deterministic data gathering is a different
   kind of work from judgment, and mixing them makes both harder to test. It is *not* to
   support a degraded mode: per §2.6, there is no such mode. Enforced by an import test.

3. **`hypothesis/` is off by default**, gated by config — matching tech design §12's staging
   and §9's false-confidence gate. When disabled, the pack says so explicitly.

### 3.2 LLM call sites

All four wired to Gemini. `GEMINI_API_KEY` from environment; absent key → the CLI errors
rather than producing a partial pack (§2.6).

| Call site | Tier | On failure |
|---|---|---|
| Alert identification fallback | Flash | Rules only; unidentified → ask the human |
| Search loop (code + knowledge) | Flash | Reported as failed |
| Hypothesis synthesis | Pro | Reported as failed |
| Knowledge extraction (write path) | Pro | Defer |

### 3.3 Search loop is shared

`hypothesis/search_loop.py` drives ripgrep over a repo path. Code search points it at the
service repo; knowledge retrieval points it at `rec-knowledge` after `git pull`. Same loop,
same truncation strategy (tech design §4.3), different corpus. No separate knowledge
retriever.

### 3.4 Simplifications relative to the first draft

- **No `security/` package.** Identity injection is one call-site rule (take from context,
  never a model parameter); path scoping is an argument passed to ripgrep. Both live where
  they are used. Injection resistance is covered by tests, not a module.
- **`writeback/pr.py` is interface-only.** Opening real PRs has side effects and can't be
  validated in a skeleton. `admission.py` and `extractor.py` carry real, testable logic.
- **`ingest/slack.py` is data structures and parsing only.** App manifest, OAuth, and event
  subscriptions are out of scope (§1).

---

## 4. Fake data

Fixtures model four alerts from real rotation history, matching tech design §3.2:

| Alert | Scenario encoded |
|---|---|
| `news-list-for-channel` p99 | server-feed cold start after deploy (benign) |
| feed channel empty | Single runaway old client (benign) |
| `get_empty_docids` | D2D failover |
| Large-scale 5xx | 2026-06-10-style: bad commit + HPA oscillation + peak traffic |

Real alert shapes keep fixtures aligned with the doc and seed the §9.1 labeled set.

---

## 5. Testing

- **`evidence/`** — real assertions against fixtures. Given the 2026-06-10 fake alert, the
  pack must contain the HPA oscillation event and the deploy correlation.
- **Provenance invariant** — a `server_logs` result must never be usable for impact
  quantification.
- **Import invariant** — `evidence/` must not import `llm/` or `hypothesis/`.
- **Failure behavior** — with no API key, the CLI errors and emits no partial pack; with a
  reachable model and a failing step, the failure report validates against its schema and
  contains no root-cause field.
- **`hypothesis/` and `writeback/`** — call contracts and failure paths, via the fake LLM
  provider.

---

## 6. Dependencies

`pydantic`, `httpx`, `typer`, `pytest`, `google-genai`, `psycopg`. Managed with `uv`.
Postgres via docker-compose. No vector database, no BM25 library, no ORM.

---

## 7. Out of scope

Real API integrations, semantic code search, production deployment, Slack app setup.
