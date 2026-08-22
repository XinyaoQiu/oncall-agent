# On-Call Triage Agent — Tech Design

NewsBreak Server team. Internal tool. Assists the rotating on-call engineer during
alert triage by aggregating evidence and proposing a ranked hypothesis.

Companion system: the Publisher Support Assistant (external, customer-facing). The two
share evaluation, tracing, and LLM gateway infrastructure and are isolated everywhere
else. See §11.

---

## 1. Positioning — evidence pack first, diagnosis second

The output is **a structured evidence pack plus a confidence-tagged hypothesis**, not a
root-cause verdict.

This distinction drives the entire design. During an incident a wrong confident answer is
worse than no answer: it sends the on-call engineer down the wrong path while the site is
down. Meanwhile ~80% of the manual effort in the first ten minutes is mechanical data
gathering that is fully automatable.

So the agent splits its output in two:

```
┌─ Evidence (deterministic, no model judgment) ──────────────┐
│  Time window, pod scaling events, CPU/memory, deploy       │
│  events, week-over-week comparison, error-rate breakdown   │
│  by app/host/path — each with its source and query         │
└────────────────────────────────────────────────────────────┘
┌─ Hypothesis (model, confidence-tagged, always citing) ─────┐
│  victim/cause call, likely root cause, suggested next probe│
│  Every claim links back to a specific evidence item.       │
└────────────────────────────────────────────────────────────┘
```

The evidence half must be correct or absent — never guessed. The hypothesis half is
allowed to be wrong, is labeled as such, and is never presented without the evidence that
produced it.

**Adoption follows from this.** A rotating on-call population will not trust a black box
on day one. An evidence pack is useful even when its hypothesis is wrong, which is what
gets the tool used long enough to earn the right to a stronger claim later.

This split is about *presentation and trust*, not availability. It does not mean the
product still works when the model is down — see §8.1: an unavailable model is an error,
and the agent declines rather than shipping a half-analysis that reads as a whole one.

---

## 2. The investigation this automates

The team's existing large-scale 5xx playbook is seven steps. The agent's scope is defined
against it explicitly:

| # | Step | Automation | Output half |
|---|---|---|---|
| 1 | Fix the time window | Full | Evidence |
| 2 | Pod scaling / CPU → identify service | Full | Evidence |
| 3 | Layered probes → causality | Partial | Evidence + suggested probe |
| 4 | **victim/cause discrimination** | Partial — highest value | Hypothesis |
| 5 | Week-over-week → find the change | Full | Evidence |
| 6 | Code-level root cause | Partial | Hypothesis |
| 7 | Quantify impact | Full (LB logs only — §5) | Evidence |

**Step 4 is where the leverage is.** The recurring failure in past incidents was
misattributing a symptom to a cause:

- memcache errors during the 2026-06-10 a4api-web incident were a *symptom* of the bad
  commit + HPA oscillation, not the cause
- web-a4api 502s on 2026-07-07 were *propagated* from an ingress DNS reload, not a
  service fault
- a single wedged pod in the gas-stations OOM series was amplified by LB circuit-breaking
  into what looked like a global 502 event

Each of these looked like a local fault and wasn't. The agent's job at step 4 is to lay
out the propagation candidates with supporting evidence, not to declare one.

Steps 1, 2, 5, 7 are pure data aggregation. Doing those four in one shot is the bulk of
the product value.

---

## 3. Alert preprocessing — two stages

The alerts this team receives are **Prometheus/Mimir PromQL alerts**, and they arrive as
**rendered Slack messages**. A human then @-mentions the agent in the thread (§12).

That shapes preprocessing into two stages with very different trust levels:

```
Slack message text
  │
  ├─ Stage 1 — identification        (rules → LLM fallback)
  │    Answers only "which alert is this", plus key labels.
  │    Approximate is fine.
  │    ⚠ Numbers in the text are reference only. They never enter the evidence pack.
  │
  └─ Stage 2 — authoritative retrieval
       alert name → Grafana / Alertmanager rule lookup → the real PromQL expression
       → reproduce at the alert's own granularity (§3.1)
```

**The Slack message is an identification signal, not a data source.** Rendered alert text
is templated: values get rounded, labels get dropped, long expressions get truncated.
Treating it as data introduces a second, untrusted measurement of a quantity the metrics
store already holds authoritatively — the same failure class §5.1 guards against.

Identification is allowed to be approximate; measurement is not. The asymmetry is in how
each fails: a wrong identification fails **loudly** at the rule-lookup step, because the
alert name won't resolve. A wrong number fails **silently**, and shows up in an incident
review as fact.

Once Stage 2 has the authoritative rule, the extraction targets are as before:

| Alert content | Extracted | Used for |
|---|---|---|
| **PromQL expression** (from the rule) | `host` / `path` / `app` / `callee` labels | Deployment + code scope (§4) |
| **Alert label dimensions** | The exact grouping the alert fired on | Reproduction query scope |
| Firing window | Start/end, ramp shape | Deploy + HPA event correlation |
| Threshold + firing value (from metrics) | Severity, ramp rate | Impact estimate scope |
| Stack trace *(when present)* | Class/method names | Direct code search terms |
| Log line *(when present)* | Template → emitting call site (§4.4) | Code localization |

### 3.1 Reproduce with the alert's own query first

Hard rule, enforced in code: **the first metric query the agent issues is a reproduction
of the alert's own PromQL**, at the alert's own label granularity.

The failure this prevents is well documented in this team's history: taking a
host/path-scoped alert and refuting it with a site-wide view. A site-wide 5xx rate can
look flat while one host is at 100% errors. The agent must never conclude "looks healthy"
from a broader aggregation than the alert used.

Implementation: the alert's expression is parsed and re-issued before any exploratory
query. Broader aggregations are permitted only *after* the alert-granularity result is in
context, and the assembled output must show both.

### 3.2 Known benign patterns are checked early

Several recurring alerts have well-understood benign root causes. Checking these first is
cheap and short-circuits a large fraction of pages:

| Alert | Leading benign cause | Cheap check |
|---|---|---|
| `news-list-for-channel` p99 | server-feed new-pod cold start after deploy | `kube_pod_start_time` vs alert window; ~10min self-heal |
| feed channel empty | Single runaway old client looping a cold endpoint | Split by `channel_id=""` and `client_ip`, not topk |
| `get_empty_docids` | D2D failover | prod-fe ingress top-IP by `x-forward-for` (downstream strips it) |

These are encoded as a checklist the agent runs, with results in the evidence pack —
including when a benign pattern is *ruled out*, which is itself informative.

### 3.3 Thread context as prior input

By the time a human @-mentions the agent, the thread usually contains discussion already —
"is this the cold-start thing again?", "CPU looks fine to me", "I think it's the comment
cluster". This is high-quality prior information and it is free.

Used for:

- Directions a human already excluded → deprioritized in ordering, and shown as such
- Services or components a human named → searched first
- Surfaced in the pack's "already established in thread" section, so the agent doesn't
  repeat back what people just said

**Hard constraint: thread context affects ordering and presentation only. It never
suppresses evidence collection.** A human saying "CPU is fine" must not cause the agent to
skip CPU collection — the human may be wrong, may have looked at the wrong dashboard, or
may have looked before the spike. The pack's value depends on being complete and
independently gathered. Prior information reorders work; it never removes it.

The @-mention itself is also a signal: a person has read the alert and decided help is
wanted. That is a better trigger than firing on every alert (§12).

---

## 4. Service and code localization

### 4.1 Deterministic deployment resolution — not an agentic search

`server` is a single binary deployed as multiple path-scoped deployments (default,
a4api-web, server-feed, …). Mapping an alert's host/path to a deployment is a **lookup,
not a search**, and the team already maintains this mapping.

```python
resolve_deployment(host=None, path=None, app_label=None)
→ {
    app_label, pod_name_pattern, replicas, traffic_share,
    code_route_paths: [...],     # where routes are registered
    grafana_dashboard_uid,
  }
```

The inverse direction matters as much:

```python
resolve_blast_radius(code_path)
→ { deployments: [...], hosts: [...], est_traffic_share }
```

"I found this code — who runs it and what breaks?" is a question the agent must answer
without guessing. Letting a model grep its way to this answer is slower and less reliable
than a table the team already has.

**Naming discipline:** the four naming dimensions (prod routing group, standalone cluster,
Grafana panel name, k8s app label) are routinely conflated. The resolver returns all of
them explicitly rather than a single ambiguous "service name".

### 4.2 Code search — agentic, lexical-first

For code, retrieval is lexical (ripgrep) plus agentic iteration:

```python
search(pattern, path_filter, max_results)   # ripgrep
read_file(path, line_range)                 # bounded window
list_dir(path)
git_log(path, since)                        # recent changes, deploy correlation
```

Identifiers are exact tokens; embedding them loses the property that makes them findable.
`grep ERR_4021` is exact where dense retrieval is approximate. The agentic loop wins over
a fixed pipeline because round two can be chosen based on round one's results.

**Known weakness, stated plainly:** this approach is strong for identifiers and exact
tokens, and weak for semantic queries ("where is payment-timeout retry handled") where
the agent doesn't know what to grep for. Engineers answer those from a mental model of the
codebase, which the agent lacks. The mitigation is §3 — alert preprocessing supplies
high-quality search terms so the agent rarely has to guess a keyword cold. Semantic code
queries remain out of scope for v1.

### 4.3 Truncation strategy — where accuracy is actually won

A 500-entry grep hit list cannot enter context. **This strategy, not prompt tuning,
determines localization accuracy.** Levers, each tested against the labeled set (§9):

- Aggregate by file rather than listing every line hit
- Rank by path priority (core service paths above scripts)
- Rank by recency — and specifically boost files touched in deploys inside the alert window
- Exclude tests, vendor, generated code (`*.pb.go` especially — this repo has a lot)
- Read window: N lines around hit vs. enclosing function (needs AST)

### 4.4 Log-template extraction

Runtime logs are formatted; source code holds the template. Resolving a log line back to
its emitting call site is deterministic and needs no LLM.

```
Runtime:  "user 12345 not found in region us-west"
Source:   fmt.Sprintf("user %s not found in region %s", uid, region)
```

**Structured logging is the easier and more common case** in this codebase —
`log.Infow("user not found", "uid", uid, "region", region)` — where the static message
string is a direct, unambiguous grep target. Handle structured logs first; treat
format-string reconstruction as the fallback for legacy call sites.

---

## 5. Data sources — and their sampling semantics

Retrieval mechanism follows the data's nature:

| Source | Mechanism | Notes |
|---|---|---|
| **Grafana / Mimir metrics** | Structured query | Primary. Steps 1, 2, 5 depend on it entirely |
| **ES `lb_access_logs`** | Structured query | **Authoritative for impact counts.** ~6 day retention |
| **ES `server_logs`** | Structured query | **Sampled ~1/8 — qualitative only.** Per-func/path breakdown, trace following |
| **K8s state** | Structured query | `kube_pod_start_time`, ReplicaSet, HPA events |
| **Code** | Lexical + agentic (§4) | Identifiers are exact tokens |
| **`rec-knowledge`** | Lexical (ripgrep) + agentic iteration | **Existing repo — reused, not rebuilt.** Same tooling as code (§6.1) |
| **Deploy history** | Structured query | Jenkins + `git_log`, correlated to alert window |

### 5.1 Sampling semantics are enforced in the tool layer, not the prompt

This is the highest-risk correctness issue in the system. Known traps:

| Trap | Consequence if unhandled |
|---|---|
| `server_logs` sampled ~1/8 | Impact quantification **understated 7-8×** |
| `lb_access_logs` ~6d retention vs `server_logs` ~7d, with a rollover boundary | Silent empty results read as "no errors" |
| Loki sharding can return a false 0 | Same |
| `X-Request-ID` lives in the access index, not `.ds-server_logs`, and carries hyphens | Trace lookups silently miss |
| `status` needs a `terms` query, not `range` | Wrong error counts |

**Every log-query tool returns its result wrapped with provenance metadata**, and the
model never sees a bare number:

```json
{
  "value": 41233,
  "source": "server_logs",
  "sampling_rate": "~1/8",
  "usable_for": ["qualitative_breakdown", "trace_following"],
  "NOT_usable_for": ["impact_quantification"],
  "retention_boundary_hit": false
}
```

Impact quantification (step 7) is hard-wired to `lb_access_logs`. The agent cannot
override this — it's a routing decision in code, not an instruction the model may weigh.
A model that reports a 7×-understated user-impact number in an incident review is worse
than one that declines to answer.

**Empty result ≠ healthy.** Every query path distinguishes "zero matching events" from
"query returned nothing because of a retention boundary, a sharding fault, or a wrong
index", and surfaces which one it was.

### 5.2 Operational records

Three distinct data classes get conflated under "history". They belong in different places:

| Data | Written by | Reviewed by | Store |
|---|---|---|---|
| **Knowledge entries** | The agent, via PR (§7) | Human review + merge | git (`rec-knowledge`) |
| **Operational records** | The agent | — | **Postgres** |

Operational records = one row per invocation: which alert, which queries were issued, what
the evidence pack contained, what hypothesis was offered at what confidence, and whether a
human later corrected it. Plus the labeled set (§9.1) and evaluation results.

**This layer is mandatory, not optional instrumentation.** Every metric in §9 —
false-confidence rate, evidence-pack completeness, time-to-diagnosis — is computable only
from these records. Without it, the entire evaluation section is unimplementable.

Postgres rather than a file or an embedded database: the agent runs as multiple replicas,
so the store must be shared and network-reachable. The §9 metrics are time-bucketed
aggregations over alert name and confidence, which is a relational workload, and evidence
packs land naturally in `jsonb`.

---

## 6. Knowledge storage — reuse `rec-knowledge`

The team already maintains `rec-knowledge`, a local markdown repo of incidents and
runbooks. **This is the source of truth. Do not build a new knowledge base.**

```
rec-knowledge (git, existing)
  ├─ git pull master before every retrieval session   ← required; the repo goes stale locally
  ├─ the agent opens PRs (§7); humans review and merge
  └─ version history, diffs, revert come free
```

Why git rather than a database: **the writer is a model, not a human.** PR review is not a
collaboration convention here — it is the safety mechanism. §7 requires model-generated
writes to be retry-safe and reversible, and PR diff / revert / attribution is exactly that.
When people author content, PR review is good practice; when a model authors it, PR review
is the control that makes the write path acceptable at all. History and blame come free,
and engineers can still edit directly.

### 6.1 Retrieval — the same lexical tooling as code search

**No vector index.** `rec-knowledge` is retrieved with the §4.2 toolset — ripgrep over a
local git repo, driven by the same agentic loop — pointed at the knowledge repo instead of
the service repo.

This replaces an earlier embedding-index design. Three reasons:

1. **The corpus is small and the queries are exact.** A few hundred markdown files, queried
   with the high-quality identifiers §3 already extracts — service names, alert names, error
   codes. That is lexical retrieval's strong case.
2. **The loop handles paraphrase better than a fixed pipeline does.** The concern that
   motivated embeddings was synonymy ("OOM" vs "memory exhaustion" vs "container killed").
   An agentic loop addresses it directly: the model sees round one's hits and re-queries with
   different wording. A pre-built index has to guess the right vocabulary up front — which is
   the same argument §4.2 already makes for code.
3. **No new infrastructure, and one less component.** Knowledge retrieval reuses the code
   search tools rather than owning a parallel implementation.

**Retrieval lives on the hypothesis side, not the evidence side** (§1). "Which past incident
resembles this one" is a judgment, not a measurement. It is not part of the deterministic
half and does not have a non-model fallback — consistent with §8.1.

**Migration signal**, should it ever be needed: not entry count, but lexical search
repeatedly missing entries a human knew were there, or metadata filtering (service + time
window + incident class) becoming the dominant access pattern.

Confluence incident write-ups are a **second** source, synced read-only. They are richer
than `rec-knowledge` entries but not engineer-editable in the same loop.

---

## 7. The write path — closing the loop

The hardest part of the system, and the part most likely to be quietly abandoned.

### 7.1 Admission gate — write less, but keep writing

**Not every alert produces a knowledge entry.** With the current page volume, routing every
alert into an extraction-and-PR flow would generate dozens of PRs a week awaiting review by
the same on-call engineers who are already busy. That flow dies in week three — the same
failure mode as "nobody updates the wiki after three months".

Admission requires **both**:

1. The incident was actually resolved (not auto-recovered and unexplained), **and**
2. It is P0/P1, **or** the incident owner explicitly tagged it as reusable

Everything else is logged for retrospective mining, not written.

### 7.2 Contradiction handling at write time

```
Incident resolved + admitted
      ▼
Extract candidate knowledge (LLM)
      ▼
Retrieve existing related entries          ← the key step
      ▼
Classify: ADD / UPDATE / CONFLICT
  ├─ ADD      → open PR
  ├─ UPDATE   → open PR superseding the old entry (not appending a second one)
  └─ CONFLICT → flag both, human decides
      ▼
PR review → merge → reindex
```

Failure modes this addresses:

| Failure | Consequence |
|---|---|
| **Contradiction** | "Restart X" and "X was decommissioned, restart Y" both in the index; retrieval picks arbitrarily |
| **False memory** | Extraction is an LLM call and can hallucinate. A wrong entry poisons **every future incident** — far worse than one wrong answer |
| **Noise accumulation** | Months of near-duplicates degrade retrieval precision |

Every mutation is a PR: diffable, revertable, attributable. Start at 100% human review;
widen automated-merge scope based on **measured** extraction accuracy, not on comfort.

A model-generated write is a side effect, and side effects must be retry-safe and
reversible — the same idempotency discipline applied to payment and settlement systems.

### 7.3 Freshness — silent expiry

On-call knowledge **expires silently**. A service is decommissioned; nobody revisits the
runbook referencing it. There is no announcement and no effective-until date.

So staleness signals *are* legitimate retrieval inputs here:

- Time-decay ranking on `last_verified`
- Scheduled scans: does the referenced service still exist? Has the entry been retrieved in
  the last N months?
- Surface entry age in the injected context and let the model weigh it

This is the **opposite** of the policy KB in the companion system, where expiry is explicit
and time-decay would be actively harmful. Different expiry mechanics, different mechanisms.
Applying either one to the other is a mistake.

---

## 8. Orchestration and budgets

```
Alert
 ├─ Reproduce alert PromQL      (structured, FIRST — §3.1)   ┐
 ├─ Deployment resolution       (table lookup, §4.1)         │
 ├─ Pod/HPA/deploy events       (structured)                 ├─ parallel
 ├─ Benign-pattern checklist    (structured, §3.2)           │
 ├─ rec-knowledge retrieval     (agentic lexical, §6.1)      │
 └─ Code search                 (agentic, multi-round, §4.2) ┘
 ▼
Assemble under context budget
 ▼
Evidence pack  +  confidence-tagged hypothesis
```

Hard limits enforced in the routing function, where the model has **no veto**:

- **Max search rounds** — set from the labeled set's rounds-vs-accuracy curve, measured
  before launch. Not a guessed constant.
- **Wall-clock budget** — this runs during an incident; slow is useless. Partial evidence
  delivered on time beats complete evidence delivered late, so the assembler emits whatever
  has returned when the budget expires and marks the rest as timed out.
- **Token budget**

### 8.1 Model tiering and failure behavior

| Call site | Latency need | Tier | On failure |
|---|---|---|---|
| Alert identification fallback | Low | Small | Rules only; unidentified → ask the human |
| Code-search iteration | Medium | Mid | Fewer rounds, wider truncation |
| Knowledge retrieval loop | Medium | Mid | Reported as failed; no fallback path |
| Hypothesis synthesis | Tolerant | Large | Reported as failed |
| Knowledge extraction (write path) | Offline | Large | Defer; never degrade quality |

**An unavailable LLM is an error, not a degraded mode.** The agent reports the failure and
declines to answer. It does not emit a partial pack that looks like a normal one.

This corrects an earlier version of this design, which treated "fall back to the evidence
pack" as a feature. It isn't. An evidence pack produced without the model is missing
victim/cause discrimination, related incident history, and code localization — but it
renders identically to a complete one: same format, same tables, same confident layout.
During an incident nobody stops to ask whether half the analysis is silently absent. A
response that looks complete and isn't is worse than a visible failure, and the on-call
context is exactly where that gap does the most damage.

**Failure reporting is two-layered:**

```
A call fails
  ├─ LLM still reachable → the model composes the failure report itself:
  │     what was collected, which step failed, what the human should do next
  └─ LLM unreachable     → deterministic error: failed step + what was collected
```

The first layer is the useful one. A model that can see the collected evidence writes a
far better handoff than a template can — "CPU and deploy data are clean, code localization
failed, suggest grepping X by hand" beats "Error: hypothesis generation failed."

**That report is schema-constrained to `collected` / `failed_step` / `reason` /
`suggested_next_action`.** There is no field for a root-cause guess. Left unconstrained, a
model asked to explain a failure will reach for "based on the available evidence, this is
probably…" — reintroducing exactly the unreliable answer this section exists to prevent.
The schema removes the option rather than prohibiting it in a prompt.

---

## 9. Evaluation

### 9.1 Build the labeled set from existing postmortems — not from future note-taking

"Spend two minutes per alert recording the answer" is the standard advice and it does not
survive contact with a rotation. It is unowned work during the busiest moment.

Instead, **construct the labeled set retroactively from existing Confluence incident
write-ups and `rec-knowledge` entries**: alert/time window → the root cause that was
eventually confirmed, plus the file or service where the answer turned out to live. This
yields dozens of high-quality labeled cases at near-zero marginal cost, from incidents that
are already documented — including the 2026-06-10 a4api-web, 2026-06-29 comment mongos,
2026-07-07 ingress webflow, gas-stations OOM series, and 2026-07-10 vote write-timeout
cases.

Ongoing capture is then an *addition* to a set that already exists, rather than the
precondition for having one.

### 9.2 Metrics, mapped to the playbook

| Step | Metric | Target basis |
|---|---|---|
| 1. Time window | Accuracy | Should be ~100%; deterministic |
| 2. Service identification | Accuracy | Deterministic given §4.1 |
| 3. Probe selection | Correct-probe rate | |
| 4. **victim/cause** | Discrimination accuracy | **Highest-value metric** |
| 5. Change correlation | Recall of the actual culprit change | |
| 6. Code localization | File + region accuracy on labeled set | |
| 7. Impact quantification | Numeric accuracy vs LB-log ground truth | Must be exact — §5.1 |

System-level:

| Metric | Note |
|---|---|
| **Time-to-diagnosis, before/after** | The metric leadership cares about |
| Median search rounds | Directly reflects truncation-strategy quality (§4.3) |
| Evidence-pack completeness | % of the 4 automatable steps returned within budget |
| **False-confidence rate** | High-confidence hypotheses that were wrong — **the safety metric** |
| Adoption / retention | Engineers using it across rotations |
| Cost per query | |

The false-confidence rate governs whether the hypothesis half stays enabled. If it can't be
held low, the deployment is **configured** to evidence-only and says so — the pack states
that hypothesis generation is disabled.

This is not the same as §8.1's failure case, and the difference matters. Here the operator
has decided the model isn't accurate enough, the change is deliberate, and readers are told
what they are getting. There the model was expected to run and didn't, and the danger is a
pack that silently omits half its analysis. A configured absence is honest; an unannounced
one is not.

---

## 10. Security and access

### 10.1 Permission filtering happens before the search

Different teams see different code and logs. Enforced in the query, never as a post-filter
and never as a prompt instruction: repo and path scopes are resolved from the requester's
identity and passed *into* ripgrep's path filter, so out-of-scope files are never read.

### 10.2 Identity is injected, never model-supplied

In the Slack entry point, the requester identity comes from the Slack user context, exactly
as `publisher_id` comes from request context in the companion system. It is never a tool
parameter the model can populate. Cross-team access becomes structurally impossible rather
than merely forbidden.

### 10.3 Break-glass

A P0 routinely requires the on-call engineer to read code they don't normally own.
Blocking that is worse than the access risk. There is an explicit elevated mode: available
only during an active declared incident, fully audit-logged, announced in the incident
channel. Designed in — not left to be worked around under pressure.

### 10.4 Injection is a real vector here

The agent reads code, logs, and knowledge entries. **Logs can contain
attacker-controlled content** — user-submitted text routinely appears in log lines. Code
comments and knowledge entries are also injection surfaces.

Mitigations: retrieved content is delimited and never treated as instruction; tool
availability is fixed at session start and cannot be expanded mid-conversation; and an
adversarial test set (injection strings in log payloads, in knowledge entries, in code
comments) runs in CI alongside the evaluation regression gate.

---

## 11. Isolation from the Publisher Support Assistant

The two systems are built by the same team and share four infrastructure components. They
are otherwise separate by construction:

| Layer | Mechanism |
|---|---|
| Tools | Disjoint sets — this agent's code/log/metric tools **do not exist** in the support assistant |
| Data | Separate indexes, separate DB users, no cross-GRANT |
| Service | Separate deployment, separate credentials |

**A tool that doesn't exist cannot be invoked, however the model is manipulated.**

### 11.1 Shared infrastructure is itself a boundary-crossing channel

The shared components — evaluation, tracing, LLM gateway, prompt versioning — are the one
place where the isolation can leak, because they legitimately see both systems' data. This
agent's traces and prompt logs contain **source code and internal runbook content**.

Requirements on the shared layer:

- **Prompt/response logs partitioned per system**, with separate access control and
  separate retention. Engineers with support-assistant access must not gain code visibility
  through the gateway's logs.
- **Trace UI access scoped per system** — spans here carry code snippets.
- **Evaluation datasets never mixed.** This agent's labeled set contains internal service
  topology; the support assistant's contains publisher data. Neither belongs in the other.

---

## 12. Rollout

The entry point is a **Slack thread @-mention**: the alert posts to the channel as it does
today, and an engineer pulls the agent in when they want it.

1. **Evidence pack on mention.** The agent reads the thread, identifies the alert (§3),
   pulls the authoritative rule, and replies in-thread. Read-only. Validates §4.1 and §5.1
   against reality at zero risk.
2. **Broaden alert coverage** — more alert types recognized in Stage 1, more benign-pattern
   checks. Still summoned, never pushed.
3. **Enable the hypothesis half**, gated on false-confidence rate against the labeled set.
4. **Enable the write path**, 100% human review, admission-gated per §7.1.

**Automatic posting on every alert fire is deliberately not on this path.** The mention is
itself a signal — a person has seen the alert and judged that help is useful. Unsolicited
posting on every page trains people to scroll past the agent, which is difficult to undo.
If auto-trigger is ever revisited, it should be scoped to specific alerts with a measured
track record, not enabled globally.

Each stage is independently useful and independently revertable. Stage 1 delivers most of
the measurable time savings, which is the argument for shipping it before anything else.

---

## Design principles applied

1. **Ask where the answer lives before choosing an architecture.** Metrics → structured
   queries. Code and runbooks → lexical search over local repos. Assuming "this is a RAG"
   leads to forcing all of it through one index.
2. **Retrieval mechanism follows the data's nature — and corpus size is part of that.**
   Identifiers want lexical; relationships want structured queries. A few hundred local
   markdown files queried by exact identifiers do not need an embedding index; an agentic
   loop over grep handles the paraphrase case that would otherwise motivate one.
3. **Correctness and safety decisions are code, not prompts.** Sampling semantics, impact-source
   routing, budget caps, permission scopes. The model advises; it does not decide.
4. **Isolation by construction, not by instruction.** Disjoint tool sets beat permission
   checks, which beat prompt instructions.
5. **Identity is injected, never model-supplied.**
6. **Source of truth is git, because the writer is a model.** PR review is the safety
   mechanism for model-generated writes, not a collaboration habit — it makes them
   reviewable, attributable, and revertable.
7. **Freshness mechanics follow expiry mechanics.** Silent expiry → decay and staleness
   scans. (Explicit expiry → date filters — that's the companion system, and swapping them
   is a mistake.)
8. **Build the labeled set from what's already written down.** Retroactive construction
   from postmortems beats prospective note-taking that nobody has time to do.
9. **A missing model is an error, not a degraded mode.** A partial answer that renders like
   a complete one is the most dangerous output an incident tool can produce. Fail visibly,
   and let the model explain its own failure when it is still reachable enough to do so.
