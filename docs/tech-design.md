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

### 1.1 Investigation is unconditional

**The agent investigates every alert the same way, whether or not it recognizes it.**
Monitoring, dashboards, logs, code, deploys — that sequence runs on alert identity being
completely unknown. Recognition and past incidents change *confidence and ordering*. They
are never a precondition for looking.

This is stated first because the natural implementation gets it backwards. It is easy to
write `if known_alert: gather_evidence()`, and the result reads as reasonable code right
up until the first alert nobody has catalogued — which is precisely the alert where an
engineer most needs the mechanical work done for them. A recognized alert is the easy
case; the agent adds least value there. Making recognition the gate optimizes for the
case that needs no optimizing.

The rule follows how an engineer actually works. Nobody encountering an unfamiliar alert
declines to open Grafana. They follow the link in the alert, look at the golden signals
for whatever service it names, check whether anything deployed, widen to the dashboard,
then read logs until something points at code. None of that requires having seen the
alert before. The team's playbook (§2) is a *general* procedure; the per-alert knowledge
in §3.2 accelerates it, and an accelerator that is also an ignition switch is a design
error.

So, concretely:

| Input | What it may do | What it may never do |
|---|---|---|
| Alert recognized in the registry | Add benign checks, seed search terms, raise confidence | Gate whether metrics are queried |
| `rec-knowledge` hit | Supply a hypothesis to test, name a precedent | Replace querying monitoring |
| Deployment resolved from the table | Scope queries precisely, name owning code | Gate whether the service is probed |
| Nothing recognized at all | Still: parse links, probe golden signals, check deploys, search code | Produce an empty evidence pack |

**An empty evidence pack is a bug, not an outcome.** If the agent gathered nothing, it
must say what it tried and why each attempt yielded nothing — never return silence that
reads as "there was nothing to find". §3.5 defines what runs when identity is unknown.

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

**This playbook is general, and so is the agent's version of it.** Every step above is
defined in terms of a time window, a service, and a set of measurements — not in terms of
which alert fired. An engineer runs this same sequence on an alert they have never seen.
The agent does too (§1.1); per-alert knowledge (§3.2, §6) makes individual steps faster and
better-founded, and its absence slows the sequence rather than preventing it.

---

## 3. Alert preprocessing — extraction, then identification

The alerts this team receives are **Prometheus/Mimir PromQL alerts**, and they arrive as
**rendered Slack messages**. A human then @-mentions the agent in the thread (§12).

That shapes preprocessing into three stages with very different trust levels. The first
is deterministic and never fails; the other two are best-effort accelerators:

```
Slack message text
  │
  ├─ Stage 0 — extraction            (deterministic, no model, cannot fail)
  │    Every label, every URL, every timestamp in the text.
  │    Alert links carry the dashboard, panel, variable bindings and — critically —
  │    the exact firing window (§3.6). This is the monitoring entry point, and it
  │    arrives whether or not anyone recognizes the alert.
  │    ⚠ Numbers in the text are reference only. They never enter the evidence pack.
  │
  ├─ Stage 1 — identification        (rules → LLM fallback, may return "unknown")
  │    Answers only "which alert is this". Approximate is fine, and *absent is fine*.
  │    "unknown" is a normal result, not an error state — investigation proceeds
  │    on Stage 0 output alone (§3.5).
  │
  └─ Stage 2 — authoritative retrieval    (best-effort)
       alert name → Grafana / Alertmanager rule lookup → the real PromQL expression
       → reproduce at the alert's own granularity (§3.1)
       No rule found → §3.1's degraded path, not a dead end.
```

**Stage 0 is the one that carries the investigation.** Stages 1 and 2 make it sharper.
Ordering them this way is deliberate: the stage that cannot fail runs first and produces
enough to work with, so the stages that *can* fail are never load-bearing.

**The Slack message is an identification signal and a pointer, not a data source.** Its
labels and links tell you *where to look*; its numbers are not measurements. Rendered alert text
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

**When the authoritative rule cannot be retrieved, this rule degrades — it does not
block.** An earlier version of this design made rule lookup the sole entry point to metric
collection, which meant an unrecognized alert produced *zero* queries: the strictness
intended to prevent a wrong number instead prevented every number. That is a worse
failure. A missing rule is a reason to widen and label, not to stop.

The fallback ladder, in order:

1. **Authoritative rule from Grafana** — reproduce exactly. Labelled `authoritative`.
2. **Registry `fallback_expr`** for a recognized alert. Labelled `registry expression`.
3. **Reconstructed from the alert link** — the linked panel or Explore view is opened
   (§3.6) and the query it represents is reissued. Labelled `reconstructed from alert
   link`.
4. **Synthesized from Stage 0 labels** — golden signals for whatever `app` / `host` /
   `path` the text named (§3.5). Labelled `synthesized — not the alert's own query`.

Every rung below the first is **explicitly labelled in the pack and in the prompt**, so
neither the model nor the reader mistakes a reconstruction for the alert's own
measurement. The honesty §3.1 exists to protect lives in that label. It never lived in
refusing to query.

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

**The checklist is additive.** An alert with no entry here gets no checklist and loses
nothing else: the §3.5 baseline still runs, the links are still parsed, the code search
still happens. Recognition buys a shortcut past the common explanations. It does not buy
permission to investigate, and its absence removes only the shortcut.

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

This section covers what *people* said in the thread. What the agent itself established
on an earlier mention is a separate input, covered in §3.7 — for a while it was not an
input at all, which made every follow-up start from nothing.

### 3.4 The mention text is an input, not just a trigger

What the engineer types when summoning the agent — "find the root cause and production
impact", "is this the cold start thing", "how many users are affected" — states what they
actually want. Discarding it and running a fixed analysis wastes the most direct signal
available about the reply's purpose.

The question is passed to the diagnosis step and answered first. When the data needed to
answer it is missing, the reply names the missing data rather than answering a different
question well.

This does not change what evidence is collected. Collection is driven by the alert; the
question shapes emphasis and ordering in the reply. Letting the question narrow collection
would reintroduce the failure §3.3 guards against — a stated belief steering the agent away
from the measurement that would have contradicted it.

### 3.5 The baseline probe — what runs when nothing is recognized

Per §1.1, an unidentified alert is investigated, not deferred. The baseline is the
general SRE opening move, driven entirely by Stage 0 output:

| Probe | Driven by | Answers |
|---|---|---|
| **Golden signals** — error rate, latency quantiles, throughput, saturation | any `app` / `host` / `path` / `service` label | Is this service actually unhealthy, and how |
| **Restart and replica history** | resolved *or* guessed workload name | Crash-looping, HPA oscillation, capacity change |
| **Deploy correlation** | alert window (§3.6) ∩ `git_log` + release events | Did something change right before this |
| **Ingress-level impact** | any `host` | How much traffic is actually affected (§5.2) |
| **Neighbour health** | dependencies of the resolved deployment | Victim or cause (§2 step 4) |
| **Log sweep** | any identifier in the text — error code, exception, message | What the service says about itself |

Each probe is independently skippable when its input is genuinely absent, and **each
skip is reported with the reason**. "No `host` label, so impact was not quantified" is a
useful line in the pack. Silence in its place is not.

When Stage 0 yields *nothing* usable — no labels, no links, no identifiers — the agent
says exactly that and asks the engineer for a service name or a dashboard link. That is
the one legitimate empty-handed outcome, and it is a question, never a diagnosis.

**Label-free naming.** The baseline must work on names absent from `DEPLOYMENTS` (§4.1).
An unresolvable `app=billing-svc` is still queried directly as a metric selector; the
pack records the workload as *unverified*, sourced from alert text rather than the
deployment table.

### 3.6 Alert links are the monitoring entry point

Rendered alerts carry URLs — the Grafana panel, the alert rule, the dashboard, the
runbook. The thread text reaches the agent whole, so these arrive for free, and they are
the most direct pointer to where the answer lives. A link says *the alerting team already
decided which view shows this failure*.

The agent already speaks to these systems over their APIs — the metrics client
authenticates with a token and issues query requests. Following a link is therefore not a
new capability. It is the existing capability aimed at a target the alert supplied.

#### Why the agent does not exhaustively parse link formats

The obvious implementation is a parser per system: Grafana dashboards, Grafana Explore,
Prometheus, Loki, Kibana, raw Elasticsearch. It does not survive contact with reality.

| System | URL shape | What a parser would have to do |
|---|---|---|
| Grafana dashboard | `/d/{uid}?panelId=3&var-app=x&from=..&to=..` | Fetch the dashboard JSON, find the panel, extract `targets[].expr`, substitute `$var` bindings — two hops and a template engine |
| Grafana Explore | `/explore?left={"queries":[…]}` | JSON embedded in a query parameter, with a versioned schema |
| Prometheus | `/graph?g0.expr=..&g0.range_input=15m` | Parse `15m`-style durations |
| Loki | `/explore?…datasource=loki` | Same shell as Grafana Explore, different query language |
| Kibana | `/app/discover#/?_g=(time:(from:..))` | **rison** encoding, not JSON — a separate dependency |
| Elasticsearch | direct | A different query DSL entirely |

That is six independently rotting adapters, each broken by an upstream URL-schema change,
before counting internal short links, SSO redirects, and in-house tools. **Exhaustive
enumeration is a losing race**, and the failure it produces is the bad kind: a stale
parser returns a malformed query, the backend answers with an empty result, and per §5.2
an empty result is indistinguishable from "no errors" unless something says otherwise.

#### The split: facts in code, query construction in the model

Underneath the format differences, every monitoring link reduces to the same four things:
*which system, what query, what time window, what scope.* Those parts do not deserve the
same treatment.

| Part | Handled by | Why |
|---|---|---|
| **Which system** | Code — host matched against configured datasources | Also the allowlist (below). One mechanism, two jobs |
| **Time window** | Code | Pure fact, and the one place a silent failure hides: `from=1755800000000` read as seconds instead of milliseconds shifts the window by decades, and the query returns empty rather than failing. Every dialect (`from=…`, `now-15m`, `range_input=15m`) normalizes to one interval |
| **The query itself** | **The model** | Six query languages, each evolving. This is where enumeration fails and where a model is genuinely strong — it reads PromQL, LogQL, KQL and DSL, and "what should I ask this panel" is a judgment |
| **Execution** | Code — one client per datasource *type* | Auth, timeouts, and §5.1 provenance wrapping live here. API contracts are stable in a way URL formats are not |

The leverage is in the last row: the agent needs **a few query clients, not six URL
parsers**. `query_metric(expr)` already exists; `query_logs` is the addition. Those
interfaces are stable because they are API contracts.

#### The `open_link` tool

Links are surfaced to the investigation loop as a tool rather than pre-digested:

```
open_link(url) → {
    system:      "grafana" | "loki" | "kibana" | "prometheus" | "unknown",
    time_window: {from, to},     ← extracted in code; reliable
    raw_params:  {…},            ← every parameter, unmodified
    hint:        "Grafana dashboard uid=abc panelId=3, var-app=server-feed",
}
```

The model reads this and decides what to issue next — `query_metric` with a PromQL
expression, `query_logs` with a LogQL selector, or nothing. **An unrecognized link is not
a dead end:** `system: "unknown"` still carries `raw_params`, and a model reading a URL's
parameters can usually tell what was being looked at. Degradation is gradual, which is
what enumeration cannot offer.

Runbook and wiki links are retrieved as text and treated as §10.4 untrusted content.

#### Reach is bounded by configured hosts, not by URL shape

A link in a Slack message is attacker-influenceable: anyone in the channel can post one,
and an alert template can be edited. `open_link` therefore resolves the host against the
**configured datasources** (`GRAFANA_URL`, `LOKI_URL`, `KIBANA_URL`, …) and will only
issue requests to those. Anything else is reported as an external link for a human to
open — never fetched.

Host matching rather than path sniffing is deliberate: `/d/` in a path proves nothing
about who is answering, and treating a path pattern as identity is how an SSRF filter gets
walked past. The host allowlist is the same table that tells the agent which client to
use, so identification and authorization are one decision instead of two that can
disagree.

---


### 3.7 The thread is a conversation, not a series of first contacts

A thread rarely contains one mention. The engineer asks something, reads the reply, and
asks a follow-up — "what about server-feed's CPU", "which file emits that metric". Each
of those only makes sense against the answer before it.

An earlier version of this design treated every mention as a fresh start. §3.3 covered
what *people* had said in the thread but nothing about what the agent itself had already
done, so a follow-up re-identified the alert, re-queried the same metrics, and re-ran
searches that had already come back empty. Measured on a two-turn thread: the follow-up
spent five rounds re-establishing what the first turn had already found. With the earlier
turn in context it took one.

**The data already existed.** Every run writes to `invocations` and `investigation_steps`
(§5.3) for evaluation. Short-term memory is those rows read back, keyed by `thread_ts` —
no new storage, and nothing retained that was not already retained. The defect was never
missing data; it was written and never read.

What is carried forward:

| Carried | Why |
|---|---|
| The previous conclusion and its confidence | The follow-up is usually about that conclusion |
| Which searches ran, and the first line each returned | Repeating a search that came back empty is the commonest way a follow-up wastes a round |

Bounded to the last few turns and the first several steps of each: a thread that runs all
afternoon must not grow the prompt without limit.

**Long-term memory is deliberately absent.** Across incidents, `rec-knowledge` already
holds what was learned, and holds it behind human review (§7). A second store of
remembered conclusions would compete with it, would not be reviewed, and would go stale
in exactly the silent way §7.3 describes. What deserves to outlive an incident goes
through a PR, rather than accumulating as a side effect of having been asked.

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

**A miss returns "unresolved", never a plausible default.** The table has a catch-all
entry (`/` → `server-default`), which makes an unknown path silently resolve to a real
deployment that does not serve it. Downstream the resulting pod and replica queries
succeed and return true data about the wrong workload — the failure renders exactly like
a success. Any resolution that lands on the catch-all, or on a host match with no path
match, is returned flagged as **low-confidence** and carries how it was reached. The
pack and the prompt both show that flag.

**Unresolved does not stop the investigation** (§1.1). It drops the probes that genuinely
need a workload identity and keeps everything driven by raw labels — §3.5's label-free
naming path. The distinction: a flagged guess is usable, an unflagged one is a trap.

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
| **Linked dashboards / panels** | `open_link` → typed client (§3.6) | Host-allowlisted; the alert names its own view |
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

### 5.2 Impact quantification

Step 7 asks how much is actually affected. Two ingress-layer queries answer it: error
requests per second scoped to the alerting host, and total requests over the same window,
so a rate can be derived rather than asserted.

**The source is fixed in the tool, not chosen by the caller.** Application logs are
sampled at roughly 1 in 8, so counting impact there understates it by nearly an order of
magnitude. That specific error is expensive because the resulting number carries into an
incident review looking authoritative, and nobody re-derives it. Routing the query is a
correctness decision, so it lives in code — the same reasoning §5.1 applies to sampling
metadata.

Scope follows the alert: a host-scoped alert produces host-scoped impact queries. An alert
with no host or deployment produces none, rather than a site-wide number that answers a
question nobody asked.

**Empty result ≠ healthy.** Every query path distinguishes "zero matching events" from
"query returned nothing because of a retention boundary, a sharding fault, or a wrong
index", and surfaces which one it was.

### 5.3 Operational records

Three distinct data classes get conflated under "history". They belong in different places:

| Data | Written by | Reviewed by | Store |
|---|---|---|---|
| **Knowledge entries** | The agent, via PR (§7) | Human review + merge | git (`rec-knowledge`) |
| **Operational records** | The agent | — | **Postgres** |

Operational records = one row per invocation: which alert, which queries were issued, what
the evidence pack contained, what hypothesis was offered at what confidence, and whether a
human later corrected it (§7.4). This is the raw trace, and it stays raw: candidate
knowledge extracted from it goes to `rec-knowledge` through a PR (§7.1), never from here
into retrieval. Plus the labeled set (§9.1) and evaluation results.

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

### 6.2 A precedent is an answer to offer, never a reason to stop looking

A past incident that resembles this one is valuable and must be surfaced early. The
standing rule is that a runbook tells you what to *check*, not what *happened* — a
precedent is a hypothesis, and hypotheses get tested against measurements.

There is one narrow exception, and it is about the engineer's time rather than the
agent's work: **when a precedent matches closely, say so immediately, with attribution,
while the investigation continues.**

```
Match detected  →  reply now: "This looks like <entry>, <date>. Root cause then: <X>."
                →  and keep querying; the pack follows
```

Three constraints make this safe:

1. **The match is decided in code, not by the model.** Same alert name, overlapping
   labels, and a metric shape consistent with the precedent. A model asked "is this the
   same as that" will lean toward yes — it is the agreeable answer, and it is exactly the
   false-confidence §9 measures.
2. **It is always attributed and dated.** "Same as the 2026-07-07 ingress webflow
   incident" lets the engineer apply their own judgment in one second. An unattributed
   "this is probably a DNS reload" does not.
3. **It never truncates collection.** The engineer decides whether the precedent settles
   it. The agent's job is to hand them the comparison, not to make it for them — the same
   reasoning as §3.3, where a human's stated belief reorders work without removing any.

**Relationship to the standing prompt rule.** The diagnosis prompt instructs the model
that a runbook "tells you what to check, not what happened", and that rule stands
unchanged — it governs the *model's* reasoning, which is where precedent-as-proof does its
damage. §6.2 operates outside that: the short-circuit is emitted by code, before and
alongside the model's analysis, and is phrased as attribution ("this resembles X") rather
than as a conclusion the model derived. The model is never told to treat a precedent as
support. The engineer is told a precedent exists.

**What is saved is the engineer's waiting, not the agent's querying.** Queries are cheap
and run in parallel; a fast, attributed pointer is what a person can act on while the rest
arrives. Skipping the measurements to save the machine effort would trade the expensive
resource for the cheap one — and would mean a precedent that happened to be wrong goes
uncontradicted, which is how a stale knowledge entry (§7.3) outlives the system it
described.

**Migration signal**, should it ever be needed: not entry count, but lexical search
repeatedly missing entries a human knew were there, or metadata filtering (service + time
window + incident class) becoming the dominant access pattern.

Confluence incident write-ups are a **second** source, synced read-only. They are richer
than `rec-knowledge` entries but not engineer-editable in the same loop.

---

## 7. The write path — closing the loop

The hardest part of the system, and the part most likely to be quietly abandoned.

### 7.1 The PR is the admission gate

**Every completed triage produces a candidate entry.** There is no separate eligibility
test upstream of it.

An earlier version of this design gated admission on severity — resolved *and* (P0/P1 *or*
tagged reusable) — to hold PR volume down. That gate solved the wrong problem. What makes a
run worth recording is whether it established something that would help next time, and
severity does not predict that. The most valuable entries come from ordinary alerts where
an engineer said "check XX" and the agent had not known to look — precisely the runs a
severity gate discards.

**The gate is merge, not extraction.** The pipeline is:

```
Every run  →  candidate entry  →  PR  →  human merges (or doesn't)
                     │                          │
              Postgres keeps it either way    only merged content
              (a run record, not knowledge)   becomes authoritative
```

This puts the boundary where the authority actually changes hands. An unmerged candidate is
not knowledge that failed a filter — it is a run record, which is what it was all along.
Nothing is lost by proposing it, and nothing becomes authoritative without a person.

**On volume.** Dozens of PRs a week is acceptable because reviewing one is a glance:
§7.2 keeps changes scoped to a single per-alert file, so a reviewer sees what this run
added to what was already known. The risk that a glance-level review cannot catch —
a plausible-looking but wrong conclusion entering the knowledge base — is addressed by
§7.2's deletion-visibility rule and by the fact that per-alert files are revised rather
than accumulated: a wrong entry is overwritten by the next run that contradicts it, instead
of surviving alongside it.

**`rec-knowledge` is the agent's external knowledge base, and its contents are
authoritative by construction.** That is why raw observations do not go in it. Run
traces — what was queried, what came back, what the model concluded, what it scored —
live in Postgres (§5.3) and are never retrieved during triage. The two stores differ in
kind, not in quality tier:

| | Content | Store | Status |
|---|---|---|---|
| Run trace | Queries issued, observations, conclusion, score | Postgres | Raw record, no authority |
| **Confirmed knowledge** | What this alert is, what to check, past root causes | **`rec-knowledge`** | **Authoritative — a human merged it** |

There is no "pending" tier inside `rec-knowledge`. Unconfirmed material is not marked as
such in the repo; it is simply still in Postgres. Adding a provisional section to an
authoritative store would dilute exactly the property that makes it worth retrieving.


### 7.2 One file per alert, revised in place

**Entries are keyed by alert, not by date.** `alerts/news-list-for-channel-p99.md` is the
file; every run touching that alert revises it.

The alternative — a new `incidents/{date}-{slug}.md` per occurrence — is what an earlier
draft specified, and it degrades steadily. A year of a recurring alert becomes forty files
that all match the same search, and the model receives forty fragments of one story instead
of one accumulated understanding. Worse, nothing ever gets corrected: a wrong conclusion
from March sits permanently beside the right one from June, and retrieval has no basis for
preferring either.

Keying by alert makes revision the default and gives §7.3's freshness machinery something
to act on: one file with a `last_verified` date, not a pile with forty.

```
Run completes  →  find alerts/<alert-name>.md
                    ├─ absent  → ADD:    create it
                    └─ present → UPDATE: rewrite it, incorporating what this run established
                  →  PR
```

`CONFLICT` remains for the case where the new finding contradicts existing content and the
agent cannot tell which is right — both are shown and a human decides. It is a distinct
outcome from UPDATE, not a failure of it.

#### Rewrite in full; make deletions visible

The extractor **rewrites the whole file**, folding new findings into the existing text
rather than appending a dated section. A file that is edited stays a single coherent
account of the alert; a file that is appended to becomes a changelog that readers must
reconstruct the current state from, and stale claims never get removed.

The cost of rewriting is that the model can quietly damage content that was already
correct — a risk that append-only does not carry. So:

**Any PR whose diff removes existing content must say so explicitly in its description:
what was removed, and why.** The removal is the part a glance-level review (§7.1) would
otherwise miss, because it is invisible inside a rewrite that looks like an improvement.
Additions can be skimmed. Deletions are stated in prose, quoting what went, so a reviewer
sees "this PR drops the claim that X" without reading the diff line by line.

This is the same principle as §1's evidence/judgment split, applied to the write path:
the dangerous operation is not the one that adds something wrong, it is the one that
removes something right while rendering identically to an ordinary edit.

#### What a per-alert entry accumulates

Retrieval feeds the diagnosis (§6.1), and the baseline probe (§3.5) reads the same file to
decide what else to collect. So the entry carries both:

| Section | Used by | Example |
|---|---|---|
| What this alert means | Diagnosis | The rule, what firing indicates |
| **What to check** | **§3.5 collection** | "Also query the upstream queue depth — 2026-08-14, the ingress metrics looked clean and the backlog was the tell" |
| Past root causes, dated | Diagnosis, §6.2 precedent | "2026-07-07: ingress DNS reload, not a service fault" |
| Ruled out | Diagnosis | Explanations that looked right before and were not |

**The "what to check" section is how a correction survives.** When an engineer says "did
you look at XX" mid-thread, that reaches this file through the §7.4 loop — and the next
firing of the same alert collects XX without anyone re-typing it. This is the mechanism by
which the agent learns to investigate better, and it needs no separate rules table: the
knowledge base the agent already retrieves is the place where "what to check" belongs.

Failure modes this addresses:

| Failure | Consequence |
|---|---|
| **Contradiction** | "Restart X" and "X was decommissioned, restart Y" both present; retrieval picks arbitrarily |
| **False memory** | Extraction is an LLM call and can hallucinate. A wrong entry poisons **every future incident** — far worse than one wrong answer |
| **Fragmentation** | Forty dated files for one alert; the model sees pieces, never the accumulated picture |
| **Silent deletion** | A rewrite drops a correct hard-won finding and reads like an ordinary edit |

Every mutation is a PR: diffable, revertable, attributable. A model-generated write is a
side effect, and side effects must be retry-safe and reversible — the same idempotency
discipline applied to payment and settlement systems.


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

**Per-alert files (§7.2) make freshness measurable rather than inferred**, and they also
change what `last_verified` must mean. A recurring alert's file is touched on every firing,
so a write timestamp would mark it fresh indefinitely while its *content* silently rots —
the specific claims inside can date from a year ago even though the file changed this
morning. `last_verified` therefore tracks when a claim was last *confirmed by an
observation*, not when the file was last written.

The corollary is the useful signal: an alert that stops firing stops being revised, and its
file ages honestly. That is exactly the entry most likely to reference a decommissioned
service — the silent expiry this section exists for — and per-alert keying makes it visible
as one stale file rather than hiding it among dated fragments.

### 7.4 The feedback loop — how a correction becomes a habit

The recurring scenario this exists for: an engineer says *"did you check the upstream queue
depth?"*, the agent checks, that turns out to be the answer — and the next time the same
alert fires, the agent checks it unprompted.

Three separate mechanisms carry that, at three different lifetimes:

```
Within the thread     engineer says "check XX"
                      → collected this run, this thread only
                      → not written anywhere; other threads unaffected
        │
        ▼  thread goes quiet for N hours
Summary posted        agent posts what it concluded and what it learned
                      → and opens the PR revising alerts/<name>.md (§7.2)
        │
        ▼
Score                 engineer rates the run
                      → §9 evaluation only; does NOT gate what was written
        │
        ▼  next firing of the same alert
Habit                 §3.5 collection reads the merged entry and queries XX
```

#### In-thread guidance is scoped to the thread

A mid-thread instruction changes what *this* run collects and nothing else. It is not
written to any store at the moment it is said, and it cannot affect another thread.

The reason is §10.4: thread text is attacker-influenceable, and a mechanism that turns a
sentence into durable behavior is a persistent injection vector. Making the in-thread
effect ephemeral removes that entirely — durable change happens later, through a PR a human
merges, which is a path that already assumes untrusted input.

Note the difference from §3.3: thread priors *reorder* work and never suppress collection.
Guidance here *adds* collection. Both are safe in the same way — neither can remove a
measurement.

#### The clock is per-thread and lives in the bot

Slack has no "thread ended" event, so the agent keeps its own timer: **each thread has a
deadline, reset on every new message in it.** When a deadline passes with no activity, that
thread's summary fires. No polling, no scanning — a timer per active thread, cancelled and
rescheduled as messages arrive.

Default N is four hours: long enough to clear a typical incident without interrupting one,
short enough that the summary still lands the same day, while the engineer remembers.
An explicit "resolved" in the thread fires it early.

This is the first component that acts without being summoned, which §12 deliberately
avoided for alerts. The distinction: §12's concern was unsolicited posting on *every alert*,
training people to scroll past. This posts once, in a thread the agent was already invited
into, after the work is over. Being summoned remains the rule for *starting* an
investigation.

**The bot needs a scheduler.** This is new architecture — the Slack app is otherwise purely
event-driven. Timers are in-memory and therefore lost on restart, which is acceptable: a
missed summary costs one learning opportunity. Pending deadlines are recoverable from
`invocations` if that turns out to matter.

#### Every run produces a candidate; the score does not gate it

**Learning is not conditional on being rated.** The summary and its PR are produced
regardless. Scoring exists to measure whether the agent is getting better (§9) — a
different question from whether this run learned something.

Coupling them would corrupt both. If a score gated the write, an unrated run would silently
teach nothing, and the learning rate would track *engineer diligence* rather than agent
quality. And §9's false-confidence rate depends on unrated runs counting as unrated — a
metric that improves when nobody is looking is worse than no metric.

So: the write path (§7.1) runs on every completed triage. The score annotates the run
record in Postgres. The PR that changes `rec-knowledge` is reviewed on its own merits by
whoever merges it, which is the actual quality gate.

#### What the summary contains

Posted to the thread, and written to be scannable — it arrives hours later, when attention
has moved on:

- What the alert turned out to be, and the agent's confidence at the time
- **What it learned** — named explicitly, in the engineer's own terms where possible:
  *"you pointed me at the queue depth; I've added that to what I check for this alert"*
- A link to the PR revising `alerts/<name>.md`, with deletions called out (§7.2)
- Rating controls

Surfacing the learned item in the summary is what makes it correctable: an engineer who
sees "I've added X" and knows X was a dead end can say so before the PR merges. That is
cheaper than discovering it three firings later, and it is the same attribution principle
as §6.2 — say where a belief came from, and a person can evaluate it in one second.

---

## 8. Orchestration and budgets

```
Alert
 ▼
Stage 0 — extract labels, links, identifiers, window   (deterministic, always runs)
 ▼
 ├─ Reproduce alert PromQL     (§3.1 ladder — degrades, never blocks)   ┐
 ├─ Baseline probe             (§3.5 — runs on labels alone)            │
 ├─ Deployment resolution      (table lookup, §4.1; may be unresolved)  │
 ├─ Pod/HPA/deploy events      (structured)                             ├─ parallel
 ├─ Benign-pattern checklist   (§3.2 — additive, may be empty)          │
 ├─ Impact quantification      (§5.2 — hard-routed to LB logs)          │
 ├─ rec-knowledge retrieval    (agentic lexical, §6.1)                  │
 └─ Code search                (agentic, multi-round, §4.2)             ┘
 ▼
Assemble under context budget
 ▼
Evidence pack  +  confidence-tagged hypothesis
```

**Nothing in that fan-out is conditional on recognizing the alert** (§1.1). Branches vary
in what they *return* — a checklist may be empty, a deployment may be unresolved, a rule
may come from rung 4 of §3.1 — and each says which. None of them is skipped because
identification came back "unknown".

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

**Overload is retried, then dropped a tier.** A 503 from a busy model is transient, and
giving up on the first one makes the agent unavailable exactly when incidents cluster. So
transient failures retry with backoff, bounded to roughly twenty seconds — during an
incident a late answer is worth little, and failing clearly beats keeping someone waiting.

If the deep tier is still overloaded after that, the request drops to the fast tier rather
than failing. **The reply says which model answered.** This is not a contradiction of the
rule above: the distinction is between an answer that is *weaker and labelled* and an
answer that is *incomplete and unlabelled*. A shallower analysis the reader can discount
is useful; half an analysis that renders like a whole one is not.

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
| **Unrecognized-alert coverage** | Probes returned when identification is "unknown" — **the §1.1 regression guard** |
| **Empty-pack rate** | Runs that gathered nothing. Target: zero. Any occurrence is a defect, not a statistic |
| Link-extraction rate | Alerts whose window and scope came from a parsed link (§3.6) rather than a guess |
| Precedent short-circuit precision | §6.2 fast answers that the completed pack later contradicted |
| Adoption / retention | Engineers using it across rotations |
| Cost per query | |

**Cold-start coverage is measured separately and deliberately.** The labeled set is built
from *documented* incidents (§9.1), so it over-represents alerts the team already
understands — the exact population where recognition-gating hides. Evaluation therefore
includes a held-out slice with the alert registry disabled, measuring what the agent
returns knowing nothing. A system that scores well only on catalogued alerts has been
tuned on its easiest cases.

**Ratings are collected in the thread, not on the command line.** The §7.4 summary posts
rating controls where the work happened, hours after it happened. A CLI verdict command
exists and will not be used: it requires an engineer to remember a run id and leave Slack
to type it, which is unowned work competing with their actual job — the same reason §9.1
rejects prospective note-taking.

This makes the rated population non-random in a way worth stating: engineers rate runs they
noticed, which skews toward memorable ones — the very good and the very wrong. Rates are
therefore reported with their denominator, and the unrated share is shown alongside rather
than hidden by a percentage.

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

**Alert links are a distinct vector, because they can cause outbound requests.** Everything
else the agent reads is inert text; a URL handed to `open_link` (§3.6) is an instruction to
contact a host. Anyone in the channel can post a link, and alert templates are editable. The
host allowlist is what contains this: reach is bounded by configured datasources, decided
before any request, and never inferred from the URL's own path. A link is data about where
to look — never authorization to look there.

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

1. **Evidence pack on mention.** The agent reads the thread, extracts labels and links
   (§3 Stage 0), pulls the authoritative rule when there is one, runs the §3.5 baseline
   regardless, and replies in-thread. Read-only. Validates §3.5, §3.6, §4.1 and §5.1
   against reality at zero risk.

   **Stage 1 is validated against unrecognized alerts specifically.** The registry starts
   nearly empty, which makes this the honest test of §1.1: if the agent is only useful on
   catalogued alerts, that shows up here, before any of it is load-bearing.
2. **Broaden alert coverage** — more alert types recognized in Stage 1, more benign-pattern
   checks. Still summoned, never pushed. This raises confidence and shortens common cases;
   per §1.1 it does not change what gets investigated, so coverage growth is an
   improvement curve rather than a prerequisite.
3. **Enable the hypothesis half**, gated on false-confidence rate against the labeled set.
4. **Enable the write path and the feedback loop** (§7.1, §7.4). Every completed triage
   proposes a PR against its per-alert file; 100% human review. The §7.4 clock, thread
   summary and rating controls ship with it — without them the loop is open, and the
   in-thread corrections that make the entries worth writing never reach the repo.

   Ship the summary *before* enabling automatic PRs if volume is a concern: the summary
   alone shows what would have been written, which is a cheap way to calibrate extraction
   quality against real threads before anything is proposed for merge.

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
10. **Prior knowledge weights judgment; it never gates investigation.** Recognizing an
    alert, matching a past incident, resolving a deployment — each makes the answer
    better-founded. None is a precondition for querying monitoring, logs, or code. The
    unfamiliar alert is where the agent is worth the most, and it is exactly the alert a
    recognition gate turns away.
11. **Split by what rots, not by what is deterministic.** The instinct to put all link
    handling in code fails on contact with six monitoring systems, each with its own URL
    format and its own upgrade cycle. Code takes the parts that are stable and factual —
    which host, which time window — because a misread timestamp fails silently. The model
    takes the part that is diverse and evolving: constructing the query. Enumerating
    formats in code is a race against every upstream release, and losing it is silent.
    (§3.6)
12. **Capability is bounded by configuration, not by inspection.** The agent may call the
    datasources it was configured with, whatever a link claims to be. Deriving trust from
    a URL's shape is how an SSRF filter gets walked past — and it makes identification and
    authorization two decisions that can disagree, where one table serves both.
13. **A degraded input degrades the output with a label attached.** A reconstructed query,
    a guessed deployment, a precedent instead of a measurement — each is usable when it
    says what it is. The danger was never the weaker input; it was the weaker input
    rendering identically to the strong one.
