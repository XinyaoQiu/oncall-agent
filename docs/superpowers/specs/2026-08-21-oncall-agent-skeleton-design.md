# On-Call Agent Skeleton — Design Spec

**Date:** 2026-08-21
**Scope:** Runnable Python skeleton implementing all four rollout stages of
`docs/tech-design.md`, with external systems behind interfaces and fake implementations.

---

## 1. Goal and non-goals

**Goal:** a skeleton that runs end-to-end against fake data sources, with real structure
for every component the tech design names. Running the CLI against a fake alert must
produce a complete evidence pack.

**Non-goals for this pass:**

- Connecting to real Grafana / Mimir / Elasticsearch / K8s / Slack
- Semantic code search (tech design §4.2 puts it out of scope for v1)
- Production deployment concerns

**Explicitly required to work:** the deterministic evidence path, end to end, with no LLM
key present.

---

## 2. Tech design amendments

Four changes to `docs/tech-design.md`, committed before implementation.

### 2.1 §3 — alert preprocessing becomes two-stage

The tech design assumes the agent receives a structured alert payload. In reality the
alert arrives as **rendered Slack message text**, and a human @-mentions the agent in the
thread.

```
Slack message text
  │
  ├─ Stage 1: alert identification  (rules → LLM fallback)
  │    Answers only "which alert is this" + key labels. Fuzzy is acceptable.
  │    ⚠ Numeric values in the text are reference only — they never enter the evidence pack.
  │
  └─ Stage 2: authoritative data retrieval
       alert name → Grafana/Alertmanager rule lookup → authoritative PromQL
       → reproduce at the alert's own granularity (§3.1, unchanged)
```

**New principle: the Slack message is an identification signal, not a data source.**
Rendered alert text may be truncated, rounded, or missing labels. Treating it as data
introduces an untrusted third measurement of the same quantity — the same failure class
§5.1 already guards against with sampling semantics. Identification is allowed to be
approximate because a wrong identification fails loudly at the rule-lookup step; a wrong
*number* fails silently in an incident review.

### 2.2 §3.3 (new) — thread context as prior input

By the time a human @-mentions the agent, the thread often contains engineer discussion
("is this the cold-start thing again?", "CPU looks fine to me"). This is high-quality
prior information and the tech design does not currently use it.

Use:

- Directions already excluded by a human → deprioritized, marked as such
- Services a human named → searched first
- Written into the evidence pack's "already known" section

**Hard constraint: thread context affects query prioritization and presentation only. It
never suppresses deterministic evidence collection.** A human saying "CPU is fine" must not
cause the agent to skip CPU collection — the human may be wrong, and the evidence pack's
value depends on being complete. Prior information reorders work; it does not remove it.

### 2.3 §12 — entry point is a Slack thread mention

| Original stage | Revised |
|---|---|
| 1. Manual slash command | 1. **Slack thread @-mention** → evidence pack |
| 2. Auto-trigger on alert fire, pushed to channel | 2. **Broaden alert coverage** (still summoned, not pushed) |
| 3. Enable hypothesis half | 3. Unchanged |
| 4. Enable write path | 4. Unchanged |

Rationale: the @-mention is itself a signal — the human has seen the alert and judged that
help is wanted. This is lower-noise than automatic posting and easier to adopt. Automatic
triggering is deferred indefinitely rather than scheduled.

### 2.4 §6.4 (new) — operational record storage

The tech design covers the knowledge base (git) and its index (derived), but omits a third
data class. Three distinct things were conflated under "history":

| Data | Written by | Reviewed by | Store |
|---|---|---|---|
| **Knowledge entries** | **The agent, via PR** (§7.2) | Human review + merge | git (`rec-knowledge`) |
| **Vector index** | Sync job | — | Qdrant (rebuildable) |
| **Operational records** | The agent | — | **SQLite** *(new)* |

Operational records = every invocation: which alert, what was queried, what evidence was
returned, what hypothesis was offered at what confidence, and whether a human corrected
it. Plus the labeled set (§9.1) and evaluation results.

**Why this layer is mandatory:** §9's metrics — false-confidence rate, evidence-pack
completeness, time-to-diagnosis — are computable *only* from these records. Without this
layer none of the tech design's evaluation section can be implemented.

**Why not git:** dozens of structured records per day, queried by aggregation over time /
alert name / confidence. That is a database workload. It would bloat the repo and still
query poorly.

**Why knowledge entries stay in git:** because the writer is a model, not a human. The PR
flow is not a collaboration convention here — it is the **safety mechanism**. §7.2 requires
model-generated writes to be retry-safe and reversible, which is exactly what PR
diff / revert / attribution provides. When humans author content, PR review is good
practice; when a model authors it, PR review is the control that makes the write path
acceptable at all.

---

## 3. Architecture

```
oncall-agent/
├── pyproject.toml
├── docker-compose.yml               # Qdrant
├── src/oncall_agent/
│   ├── models.py                    # Alert, Evidence, EvidencePack, Hypothesis
│   ├── config.py                    # deployment shard table, known-alert registry
│   │
│   ├── ingest/                      # Stage 1 entry
│   │   ├── slack.py                 # @-mention handling, thread fetch
│   │   ├── identify.py              # alert identification: rules → LLM fallback
│   │   └── thread_context.py        # prior extraction (§2.2)
│   │
│   ├── sources/                     # external systems: Protocol + fakes
│   │   ├── base.py                  # Protocol definitions
│   │   ├── grafana.py               # rule lookup + PromQL query
│   │   ├── elasticsearch.py         # provenance-wrapped log queries
│   │   ├── k8s.py                   # pod / HPA / deploy events
│   │   ├── knowledge.py             # rec-knowledge: BM25 + vector, RRF
│   │   └── fakes.py                 # fixtures for 4 real alert scenarios
│   │
│   ├── evidence/                    # deterministic — imports no LLM module
│   │   ├── steps.py                 # playbook steps 1, 2, 5, 7
│   │   ├── benign.py                # §3.2 benign-pattern checklist
│   │   ├── deployment.py            # §4.1 resolve_deployment / blast_radius
│   │   └── assembler.py             # budget-bounded assembly
│   │
│   ├── hypothesis/                  # Stage 3, disabled by default
│   │   ├── generator.py             # confidence-tagged synthesis
│   │   └── code_search.py           # §4.2 agentic lexical loop
│   │
│   ├── writeback/                   # Stage 4
│   │   ├── admission.py             # §7.1 admission gate
│   │   ├── extractor.py             # extract + ADD/UPDATE/CONFLICT
│   │   └── pr.py                    # opens the PR (the agent is the author)
│   │
│   ├── llm/                         # provider abstraction
│   │   ├── base.py                  # Protocol
│   │   ├── gemini.py                # Flash + Pro tiers
│   │   └── fake.py                  # deterministic, for tests
│   │
│   ├── storage/                     # §2.4
│   │   ├── schema.sql
│   │   └── records.py               # invocation records, labeled set, eval results
│   │
│   ├── security/                    # §10 identity injection, path scoping, injection tests
│   └── cli.py
└── tests/
```

### 3.1 Three structural invariants

These are the design constraints that must hold in code, not just in the folder layout:

1. **`sources/elasticsearch.py` always returns provenance-wrapped results.** A `server_logs`
   query result carries `sampling_rate` and `NOT_usable_for`; impact quantification is
   hard-routed to `lb_access_logs`. The model never sees a bare number. (tech design §5.1)

2. **`evidence/` imports nothing from `llm/` or `hypothesis/`.** This makes §8.1's
   "degrade to evidence pack when the LLM is down" a structural fact rather than a promise.
   Enforced by a test that inspects imports.

3. **`hypothesis/` is off by default**, gated by config — matching §12's staging and §9.2's
   false-confidence gate.

### 3.2 LLM call sites

All four wired to Gemini, each with a defined degradation path:

| Call site | Tier | Degradation |
|---|---|---|
| Alert identification fallback | Flash | Rule matching only; unknown → ask the human |
| Code search loop | Flash | Fewer rounds, wider truncation |
| Hypothesis synthesis | Pro | **Evidence pack only** |
| Knowledge extraction (write path) | Pro | Defer; never degrade quality |

Key from `GEMINI_API_KEY`. Absent key → fake provider, and the evidence path still runs.

### 3.3 Retrieval

Qdrant via docker-compose, plus BM25 (`rank-bm25`), fused with RRF per tech design §5.
Embeddings via Gemini's embedding endpoint.

---

## 4. Fake data

Fixtures model four alerts from real rotation history, matching the benign-pattern table
in tech design §3.2:

| Alert | Scenario encoded |
|---|---|
| `news-list-for-channel` p99 | server-feed cold start after deploy (benign) |
| feed channel empty | Single runaway old client (benign) |
| `get_empty_docids` | D2D failover |
| Large-scale 5xx | 2026-06-10-style: bad commit + HPA oscillation + peak traffic |

Using real alert shapes keeps fixtures aligned with the doc and gives the §9.1 labeled set
a starting point.

---

## 5. Testing

- **`evidence/`** — real assertions against fixtures. Given the 2026-06-10 fake alert, the
  pack must contain the HPA oscillation event and the deploy correlation.
- **Provenance invariant** — a `server_logs` result must never be usable for impact
  quantification; asserted directly.
- **Import invariant** — `evidence/` must not import `llm/` or `hypothesis/`.
- **Degradation** — with no API key, the CLI still emits a complete evidence pack.
- **`hypothesis/` and `writeback/`** — call contracts and degradation paths only, via the
  fake LLM provider.

---

## 6. Dependencies

`pydantic`, `httpx`, `typer`, `pytest`, `google-genai`, `qdrant-client`, `rank-bm25`.
Managed with `uv`. Qdrant runs via docker-compose.

---

## 7. Out of scope

Real API integrations, semantic code search, production deployment, Slack app manifest and
OAuth setup.
