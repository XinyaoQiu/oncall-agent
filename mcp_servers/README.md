# MCP servers

Each server is a separate process. That is the point, not a packaging detail: the domain
code these servers wrap is entirely synchronous (`httpx.Client`, `subprocess.run`), and spec
§2.1 says a 30-second Grafana timeout on the Socket Mode event loop stops Slack acks and
gets the app throttled off new events. Out here, blocking is local and harmless — the agent
reaches these servers over async streamable-http.

## Ports

| Server | Port | Path | Status | Data |
|---|---|---|---|---|
| `grafana_server.py` | 8005 | `/mcp` | implemented | real when `GRAFANA_URL` is set, fixtures otherwise |
| `logs_server.py` | 8006 | `/mcp` | planned (spec §3) | ES / Loki |
| `monitor_server.py` | 8004 | `/mcp` | planned, port from `../oncall-agent-new` | synthetic |
| `cls_server.py` | 8003 | `/mcp` | planned, port from `../oncall-agent-new` | synthetic |

The agent dials only the servers named in `MCP_ENABLED` (see `app/config.py`; default
`grafana`). A URL that is configured but not enabled is not dialled — an unreachable MCP
server should be a choice, not a surprise at first tool load.

## Running

```bash
cd /home/xyqiu/Project/oncall-agent

# module form is preferred: no sys.path bootstrap needed
uv run python -m mcp_servers.grafana_server            # 127.0.0.1:8005/mcp
uv run python -m mcp_servers.grafana_server --port 8105

# script form works too
uv run python mcp_servers/grafana_server.py --help

# exercise every tool once and exit — no server, no network
uv run python -m mcp_servers.grafana_server --self-test
```

Point the agent at a running server with `MCP_GRAFANA_URL` and `MCP_ENABLED`:

```bash
MCP_ENABLED=grafana MCP_GRAFANA_URL=http://localhost:8005/mcp uv run oncall-api
```

## grafana_server.py

| Tool | Arguments | Backing query |
|---|---|---|
| `fetch_alert_rule` | `alert_name` | `GET /api/v1/rules`, matched case-insensitively by rule name |
| `query_metric` | `expr`, `minutes=60`, `step="1m"` | `GET /api/v1/query_range`, window ending now |
| `replica_count` | `app_label`, `minutes=60` | `kube_deployment_status_replicas{deployment="…"}` |
| `pod_starts` | `app_label`, `minutes=60` | `kube_pod_start_time{pod=~"…-.*"}` |

`fetch_alert_rule` is why the alert text is never parsed for numbers: the rule store holds
the expression that actually fired, and rendered alert text rounds values and drops labels.

### The response envelope

MCP has no provenance field, so every response carries its own:

```json
{
  "source": "query_metric",
  "synthetic": false,
  "query": "sum(rate(feed_empty_total[5m])) by (channel_id)",
  "series": [{"labels": {"channel_id": ""}, "points": [[1787786240.0, 5.0]]}],
  "error": null
}
```

- **`source`** names the tool, so `app/tools/registry.py` can look up the matching
  `SourceContract` from `config/sources.yaml` and render its caveat in the same string as the
  number. `config/sources.yaml` declares `query_metric` and `fetch_alert_rule`;
  `replica_count` and `pod_starts` are not declared, so they land on the default-deny
  contract — qualitative only, never an impact source. Declaring them is a one-line addition
  to that file, and the default is deliberately the safe direction.
- **`synthetic`** is true whenever `GRAFANA_URL` is unset and the numbers came from the
  fixture generator. A fixture ramp and a real ramp look identical, so this flag is the only
  thing standing between a demo and a citation in an incident review. It flows to
  `state["used_synthetic"]` and out as the renderer's banner.
- **`error`** distinguishes a query that failed from a service that returned nothing. Both
  produce an empty `series`, and only one of them is about the service. Failures never come
  back as a bare empty result.

### Fixture mode

With no `GRAFANA_URL`, `query_metric` answers from shapes modelled on real incidents: HPA
oscillation during the 2026-06-10 a4api-web outage, and server-feed cold start after a
deploy. The fixtures **honour their own selectors** — `_scoped()` drops series the
expression's `host=~` selector excludes, and `_matching_workload()` drops pods belonging to a
workload the `deployment=`/`pod=~` selector did not name:

```bash
uv run python -m mcp_servers.grafana_server --self-test   # pod_starts("billing-svc") → 0 series
```

That is not fastidiousness. Fixture data that ignores its selector teaches the model that
scoping does not matter, and hands back another service's pods under the name of the one that
was queried — which is exactly the failure mode this repo exists to prevent.

## Adding a server

1. Copy the shape of `grafana_server.py`: `mcp = FastMCP(...)`, `@mcp.tool()` over a
   `@log_tool_call`, an `argparse` `__main__` running `streamable-http`.
2. Return dicts carrying `source` and `synthetic`. Without them the tool's results reach the
   agent unlabelled, and unlabelled is where wrong numbers come from.
3. Add the tool names to `config/sources.yaml`. Until you do, they are qualitative-only —
   adding an MCP server must not be able to silently add an impact source.
4. Add the port to the table above and a URL field to `app/config.py`.
