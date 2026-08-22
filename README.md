# oncall-agent

An on-call triage assistant. It sits in Slack, and when you @-mention it in an alert
thread it identifies the alert, pulls the authoritative rule and metrics, searches past
incidents, and replies with a diagnosis. When an incident is resolved, it can draft a
knowledge entry and open a PR against `rec-knowledge` for review.

Design: [docs/tech-design.md](docs/tech-design.md).

## How it works

```
Slack alert thread
   │  @oncall-agent
   ▼
Identify the alert          rules first, model as fallback
   ▼
Fetch the authoritative rule from Grafana      ← the Slack text is never the data source
   ▼
Resolve host/path → deployment                 ← a lookup, not a search
   ▼
Query metrics + search rec-knowledge + read thread priors
   ▼
Diagnose (Gemini)           confidence-tagged, every claim cites its evidence
   ▼
Reply in thread
```

Two ideas run through this:

**The Slack message identifies the alert; it never supplies the numbers.** Rendered alert
text rounds values and drops labels. A wrong identification fails loudly at rule lookup; a
wrong number fails silently and ends up in the incident review as fact.

**An unavailable model is an error, not a degraded mode.** A reply that silently omits
half its analysis renders identically to a complete one, and nobody re-reads it during an
incident to check.

## Setup

```bash
uv sync
```

Environment:

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | yes | Analysis |
| `SLACK_BOT_TOKEN` | for Slack | `xoxb-…` |
| `SLACK_APP_TOKEN` | for Slack | `xapp-…`, Socket Mode |
| `KNOWLEDGE_REPO` | no | Path to a `rec-knowledge` clone |
| `GRAFANA_URL` / `GRAFANA_TOKEN` | no | Real metrics; sample data is used without them |
| `USE_SAMPLE_METRICS` | no | Force sample data |

## Usage

```bash
# Deterministic evidence only — no model call, no API key needed
uv run oncall-agent evidence --sample "[FIRING] news-list-for-channel p99 app=server-feed"

# Full analysis
uv run oncall-agent analyze --sample "[FIRING] High 5xx rate host=www.newsbreak.com"

# Search past incidents
uv run oncall-agent search "cold start"

# Run the Slack bot
uv run oncall-agent serve
```

In Slack:

- `@oncall-agent` in an alert thread → diagnosis
- `@oncall-agent record this` once resolved → drafts an entry and opens a PR

## Creating the Slack app

1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. **Socket Mode** → enable → generate an app-level token with `connections:write` →
   this is `SLACK_APP_TOKEN` (`xapp-…`)
3. **OAuth & Permissions** → bot token scopes: `app_mentions:read`, `channels:history`,
   `groups:history`, `chat:write`
4. **Event Subscriptions** → enable → subscribe to bot event `app_mention`
5. **Install to Workspace** → copy the bot token → `SLACK_BOT_TOKEN` (`xoxb-…`)
6. Invite the bot into your alert channel: `/invite @oncall-agent`

Socket Mode means no public URL and no ingress — it dials out to Slack.

## Layout

```
src/oncall_agent/
├── models.py           data models
├── config.py           settings, deployment shard table
├── alerts.py           known alerts, benign patterns
├── llm.py              Gemini client
├── pipeline.py         orchestration
├── slack_app.py        Slack entry point
├── cli.py              command line
├── sources/            grafana, knowledge repo, sample metrics
└── analysis/           identify, thread priors, evidence, diagnose, writeback
```

`analysis/evidence.py` makes no model calls: gathering measurements is different work
from interpreting them, and keeping them apart makes both testable.

## Tests

```bash
uv run pytest
```

The suite covers everything that doesn't need a model — identification, deployment
resolution, evidence gathering against sample data, and the rule that an empty metric
result is never reported as healthy.
