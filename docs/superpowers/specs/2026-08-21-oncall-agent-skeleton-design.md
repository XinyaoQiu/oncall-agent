# On-Call Agent Skeleton — Design Spec

**Date:** 2026-08-21
**Scope:** Runnable Python skeleton implementing all four rollout stages of
`docs/tech-design.md`, with external systems behind interfaces and fake implementations.

The tech design was amended alongside this spec (§2). This document covers the skeleton;
the amendments are already merged into `docs/tech-design.md`.

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
