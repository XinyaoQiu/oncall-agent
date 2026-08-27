"""Grafana / Mimir MCP server: alert rule lookup and PromQL range queries.

This process exists so the synchronous `httpx.Client` has somewhere to live. Spec §2.1: the
domain layer is entirely sync, and a 30s Grafana timeout on the Socket Mode event loop stops
Slack acks and gets the app throttled off new events. Here the blocking is local — the agent
talks to this server over async HTTP and nothing it does can stall the WebSocket.

Two things every response carries, because MCP itself has no provenance field and a result
that crosses this boundary is otherwise just a name and a shape:

- `source`, naming the tool, so `app/tools/registry.py` can look up the right
  `SourceContract` and print its caveat in the same string as the number.
- `synthetic`, true whenever GRAFANA_URL is unset and the numbers came from the fixture
  generator rather than a metrics store. A fixture ramp and a real ramp look identical.

Failures return `error` rather than an empty `series`. A query that failed and a service
that returned nothing are different facts, and only one of them is about the service.
"""

import argparse
import functools
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP
from loguru import logger

if not __package__:  # started as a plain script: the repo root is not on sys.path yet
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

mcp = FastMCP("Grafana")


def log_tool_call(func):
    """Log the call, its arguments and whether it came back — one line per boundary crossing."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger.info(f"tool={func.__name__} args={args} kwargs={kwargs}")
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            logger.error(f"tool={func.__name__} raised {type(exc).__name__}: {exc}")
            raise
        if isinstance(result, dict):
            logger.info(
                f"tool={func.__name__} ok "
                f"synthetic={result.get('synthetic')} "
                f"series={len(result.get('series') or [])} "
                f"error={result.get('error')}"
            )
        return result

    return wrapper


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

_client: httpx.Client | None = None


def _use_sample() -> bool:
    return not get_settings().grafana_url


def _http() -> httpx.Client:
    global _client
    if _client is None:
        settings = get_settings()
        _client = httpx.Client(
            base_url=settings.grafana_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.grafana_token}"},
            timeout=30.0,
        )
    return _client


def _envelope(source: str, **fields: Any) -> dict[str, Any]:
    return {"source": source, "synthetic": _use_sample(), **fields}


def _failure(source: str, query: str, error: str) -> dict[str, Any]:
    return _envelope(source, query=query, series=[], error=error)


# --------------------------------------------------------------------------
# fixtures — shapes modelled on real incidents: HPA oscillation during the
# 2026-06-10 a4api-web outage, and server-feed cold start after a deploy.
# --------------------------------------------------------------------------

_RULES: dict[str, dict[str, Any]] = {
    "news-list-for-channel-p99": {
        "name": "news-list-for-channel-p99",
        "expression": (
            "histogram_quantile(0.99, sum(rate(server_api_delay_bucket"
            '{path="/Website/channel/news-list-for-channel"}[5m])) by (le, app)) > 2'
        ),
        "duration": "5m",
        "labels": {"severity": "warning", "team": "server"},
        "annotations": {"summary": "news-list-for-channel p99 above 2s"},
    },
    "feed-channel-empty": {
        "name": "feed-channel-empty",
        "expression": "sum(rate(feed_empty_total[5m])) by (channel_id) > 100",
        "duration": "10m",
        "labels": {"severity": "warning"},
        "annotations": {"summary": "feed returning empty channels"},
    },
    "get-empty-docids": {
        "name": "get-empty-docids",
        "expression": "sum(rate(get_empty_docids_total[5m])) by (host) > 50",
        "duration": "5m",
        "labels": {"severity": "warning"},
        "annotations": {"summary": "empty docids returned"},
    },
    "large-scale-5xx": {
        "name": "large-scale-5xx",
        "expression": (
            'sum(rate(nginx_ingress_controller_requests{status=~"5.."}[5m])) by (host) '
            "/ sum(rate(nginx_ingress_controller_requests[5m])) by (host) > 0.05"
        ),
        "duration": "2m",
        "labels": {"severity": "critical"},
        "annotations": {"summary": "5xx rate above 5%"},
    },
}

Point = list[float]
SeriesDict = dict[str, Any]


def _series(labels: dict[str, str], points: list[Point]) -> SeriesDict:
    return {"labels": labels, "points": points}


def _ramp(start: float, end: float, n: int = 60) -> list[Point]:
    now = time.time()
    return [[now - (n - i) * 60, start + (end - start) * (i / max(n - 1, 1))] for i in range(n)]


def _oscillating(base: float, swing: float, n: int = 60) -> list[Point]:
    """HPA thrash: repeated scale up/down rather than a smooth ramp."""
    now = time.time()
    return [[now - (n - i) * 60, base + (swing if (i // 4) % 2 else -swing)] for i in range(n)]


def _hosts_in(expr: str) -> set[str]:
    """Hosts named by a host=~"..." selector, if any."""
    match = re.search(r'host=~"([^"]+)"', expr)
    return set(match.group(1).split("|")) if match else set()


def _workload_in(expr: str) -> str | None:
    """The workload a kube_* selector names, if any."""
    match = re.search(r'(?:deployment|pod)=~?"([^"]+)"', expr)
    return match.group(1).rstrip("-.*") if match else None


def _matching_workload(expr: str, series: list[SeriesDict]) -> list[SeriesDict]:
    """Drop series belonging to a workload the selector did not ask for.

    Fixtures that answer regardless of their own selector would teach the reader that scoping
    does not matter — and here they would hand back another service's pods under the name of
    the one that was queried, which is the exact failure this repo exists to prevent.
    """
    wanted = _workload_in(expr)
    if not wanted:
        return series
    return [
        s for s in series if any(str(v).startswith(wanted) for v in s["labels"].values())
    ]


def _scoped(expr: str, series: list[SeriesDict]) -> list[SeriesDict]:
    """Drop series the expression's host selector excludes."""
    hosts = _hosts_in(expr)
    if not hosts:
        return series
    return [s for s in series if s["labels"].get("host", "") in hosts]


def _sample_query(expr: str) -> list[SeriesDict]:
    """Plausible fixture data keyed off what the expression mentions."""
    lowered = expr.lower()

    if "kube_pod_start_time" in lowered:
        now = time.time()
        return _matching_workload(
            expr,
            [
                _series({"pod": "server-feed-7d9c4b-x8k2n"}, [[now - 600, now - 600]]),
                _series({"pod": "server-feed-7d9c4b-m4p1q"}, [[now - 540, now - 540]]),
            ],
        )

    if "kube_deployment_status_replicas" in lowered:
        return _matching_workload(
            expr,
            [_series({"deployment": "server-a4api-web"}, _oscillating(12, 8))],
        )

    if "server_api_delay_bucket" in lowered or "histogram_quantile" in lowered:
        return [
            _series({"app": "server-feed"}, _ramp(0.4, 3.2)),
            _series({"app": "server-a4api-default"}, _ramp(0.35, 0.42)),
        ]

    if "nginx_ingress_controller_requests" in lowered:
        if "5.." in expr:
            return _scoped(
                expr,
                [
                    _series({"host": "www.newsbreak.com"}, _ramp(0.4, 128.0)),
                    _series({"host": "api.newsbreak.com"}, _ramp(0.2, 1.1)),
                ],
            )
        return _scoped(
            expr,
            [
                _series({"host": "www.newsbreak.com"}, _ramp(720.0, 690.0)),
                _series({"host": "api.newsbreak.com"}, _ramp(4100.0, 4050.0)),
            ],
        )

    if "5.." in expr or "5xx" in lowered:
        return [
            _series({"host": "www.newsbreak.com"}, _ramp(0.001, 0.18)),
            _series({"host": "api.newsbreak.com"}, _ramp(0.001, 0.004)),
        ]

    if "feed_empty_total" in lowered:
        return [
            _series({"channel_id": ""}, _ramp(5, 320)),
            _series({"channel_id": "k26164"}, _ramp(8, 12)),
        ]

    if "get_empty_docids" in lowered:
        return [_series({"host": "prod-fe"}, _ramp(10, 140))]

    return []


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


@mcp.tool()
@log_tool_call
def fetch_alert_rule(alert_name: str) -> dict[str, Any]:
    """Look up an alert's authoritative PromQL expression in Grafana / Mimir.

    The Slack message identifies the alert; it never supplies the numbers. Rendered alert
    text rounds values and drops labels, so treating it as data would introduce a second,
    untrusted measurement of something the metrics store already holds.

    Args:
        alert_name: the rule name as it appears in Grafana, e.g. "large-scale-5xx".

    Returns:
        source, synthetic, alert_name, found, rule ({name, expression, duration, labels,
        annotations}) or None, and error when the lookup itself failed. `found: false` with
        no error means the rule store answered and does not have this rule.
    """
    if _use_sample():
        rule = _RULES.get(alert_name)
        return _envelope(
            "fetch_alert_rule",
            alert_name=alert_name,
            found=rule is not None,
            rule=rule,
            error=None,
        )

    try:
        resp = _http().get("/api/v1/rules")
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return _envelope(
            "fetch_alert_rule",
            alert_name=alert_name,
            found=False,
            rule=None,
            error=f"rule lookup failed: {exc}",
        )

    for group in payload.get("data", {}).get("groups", []):
        for rule in group.get("rules", []):
            if rule.get("name", "").lower() == alert_name.lower():
                return _envelope(
                    "fetch_alert_rule",
                    alert_name=alert_name,
                    found=True,
                    rule={
                        "name": rule["name"],
                        "expression": rule.get("query", ""),
                        "duration": rule.get("duration"),
                        "labels": rule.get("labels", {}),
                        "annotations": rule.get("annotations", {}),
                    },
                    error=None,
                )

    return _envelope(
        "fetch_alert_rule", alert_name=alert_name, found=False, rule=None, error=None
    )


@mcp.tool()
@log_tool_call
def query_metric(expr: str, minutes: int = 60, step: str = "1m") -> dict[str, Any]:
    """Run a PromQL range query ending now.

    Args:
        expr: the PromQL expression, e.g. 'sum(rate(http_requests_total[5m])) by (host)'.
        minutes: how far back the window reaches from now. Default 60.
        step: resolution, e.g. "1m" or "5m". Default "1m".

    Returns:
        source, synthetic, query, series (each {labels, points: [[epoch_seconds, value]]}),
        and error. An empty `series` with `error: null` means the query ran and matched
        nothing — which is a measurement outcome, not a healthy service.
    """
    if _use_sample():
        return _envelope("query_metric", query=expr, series=_sample_query(expr), error=None)

    end_ts = time.time()
    try:
        resp = _http().get(
            "/api/v1/query_range",
            params={
                "query": expr,
                "start": end_ts - minutes * 60,
                "end": end_ts,
                "step": step,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return _failure("query_metric", expr, str(exc))

    if payload.get("status") != "success":
        return _failure("query_metric", expr, payload.get("error", "unknown error"))

    series = [
        _series(
            {str(k): str(v) for k, v in item.get("metric", {}).items()},
            [[float(ts), float(val)] for ts, val in item.get("values", [])],
        )
        for item in payload.get("data", {}).get("result", [])
    ]
    return _envelope("query_metric", query=expr, series=series, error=None)


@mcp.tool()
@log_tool_call
def replica_count(app_label: str, minutes: int = 60) -> dict[str, Any]:
    """Replica count over time for one deployment — reveals HPA oscillation.

    Args:
        app_label: the deployment name, e.g. "server-a4api-web". Resolve it first; a guessed
            label returns true data about the wrong workload.
        minutes: window length. Default 60.

    Returns:
        Same envelope as query_metric, with source "replica_count".
    """
    expr = f'kube_deployment_status_replicas{{deployment="{app_label}"}}'
    result = query_metric(expr, minutes=minutes)
    return {**result, "source": "replica_count", "app_label": app_label}


@mcp.tool()
@log_tool_call
def pod_starts(app_label: str, minutes: int = 60) -> dict[str, Any]:
    """Pod start times for one workload — the cheap check for deploy-warmup alerts.

    Args:
        app_label: the deployment name, e.g. "server-feed". The pod selector is derived from
            it, so a guessed label silently returns another service's pods.
        minutes: window length. Default 60.

    Returns:
        Same envelope as query_metric, with source "pod_starts". Point values are epoch
        seconds, not magnitudes — read them as ages.
    """
    expr = f'kube_pod_start_time{{pod=~"{app_label}-.*"}}'
    result = query_metric(expr, minutes=minutes)
    return {**result, "source": "pod_starts", "app_label": app_label}


def _self_test() -> int:
    checks = [
        fetch_alert_rule("large-scale-5xx"),
        fetch_alert_rule("no-such-alert"),
        query_metric("sum(rate(feed_empty_total[5m])) by (channel_id)"),
        replica_count("server-a4api-web"),
        pod_starts("server-feed"),
        pod_starts("billing-svc"),
    ]
    for result in checks:
        print(json.dumps({k: v for k, v in result.items() if k != "series"}, default=str))
        print(f"  series={len(result.get('series') or [])}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8005)
    parser.add_argument("--path", default="/mcp")
    parser.add_argument("--self-test", action="store_true", help="exercise the tools and exit")
    args = parser.parse_args()

    if args.self_test:
        raise SystemExit(_self_test())

    mcp.run(transport="streamable-http", host=args.host, port=args.port, path=args.path)
