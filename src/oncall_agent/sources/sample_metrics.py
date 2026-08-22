"""Sample metric data, so the full pipeline runs without a reachable Grafana.

Shapes are modelled on real incidents: HPA oscillation during the 2026-06-10 a4api-web
outage, and server-feed cold start after a deploy.
"""

import re
import time

from ..models import AlertRule, MetricResult, MetricSeries

_RULES = {
    "news-list-for-channel-p99": AlertRule(
        name="news-list-for-channel-p99",
        expression=(
            'histogram_quantile(0.99, sum(rate(server_api_delay_bucket'
            '{path="/Website/channel/news-list-for-channel"}[5m])) by (le, app)) > 2'
        ),
        duration="5m",
        labels={"severity": "warning", "team": "server"},
        annotations={"summary": "news-list-for-channel p99 above 2s"},
    ),
    "feed-channel-empty": AlertRule(
        name="feed-channel-empty",
        expression='sum(rate(feed_empty_total[5m])) by (channel_id) > 100',
        duration="10m",
        labels={"severity": "warning"},
        annotations={"summary": "feed returning empty channels"},
    ),
    "get-empty-docids": AlertRule(
        name="get-empty-docids",
        expression='sum(rate(get_empty_docids_total[5m])) by (host) > 50',
        duration="5m",
        labels={"severity": "warning"},
        annotations={"summary": "empty docids returned"},
    ),
    "large-scale-5xx": AlertRule(
        name="large-scale-5xx",
        expression=(
            'sum(rate(nginx_ingress_controller_requests{status=~"5.."}[5m])) by (host) '
            '/ sum(rate(nginx_ingress_controller_requests[5m])) by (host) > 0.05'
        ),
        duration="2m",
        labels={"severity": "critical"},
        annotations={"summary": "5xx rate above 5%"},
    ),
}


def rule_for(alert_name: str) -> AlertRule | None:
    return _RULES.get(alert_name)


def _ramp(start: float, end: float, n: int = 60) -> list[tuple[float, float]]:
    now = time.time()
    return [(now - (n - i) * 60, start + (end - start) * (i / max(n - 1, 1))) for i in range(n)]


def _oscillating(base: float, swing: float, n: int = 60) -> list[tuple[float, float]]:
    """HPA thrash: repeated scale up/down rather than a smooth ramp."""
    now = time.time()
    return [
        (now - (n - i) * 60, base + (swing if (i // 4) % 2 else -swing))
        for i in range(n)
    ]


def _hosts_in(expr: str) -> set[str]:
    """Hosts named by a host=~"..." selector, if any."""
    match = re.search(r'host=~"([^"]+)"', expr)
    return set(match.group(1).split("|")) if match else set()


def _scoped(result: MetricResult) -> MetricResult:
    """Drop series the expression's host selector excludes.

    Sample data that ignores its own selector would teach the model that scoping does
    not matter — the opposite of the habit this tool is meant to reinforce.
    """
    hosts = _hosts_in(result.query)
    if not hosts:
        return result
    result.series = [s for s in result.series if s.labels.get("host", "") in hosts]
    return result


def query(expr: str, *, minutes: int = 60) -> MetricResult:
    """Return plausible sample data keyed off what the expression mentions."""
    lowered = expr.lower()

    if "kube_pod_start_time" in lowered:
        # Recent starts: consistent with a deploy inside the alert window.
        now = time.time()
        return MetricResult(
            query=expr,
            source="sample",
            series=[
                MetricSeries(
                    labels={"pod": "server-feed-7d9c4b-x8k2n"},
                    points=[(now - 600, now - 600)],
                ),
                MetricSeries(
                    labels={"pod": "server-feed-7d9c4b-m4p1q"},
                    points=[(now - 540, now - 540)],
                ),
            ],
        )

    if "kube_deployment_status_replicas" in lowered:
        return MetricResult(
            query=expr,
            source="sample",
            series=[
                MetricSeries(
                    labels={"deployment": "server-a4api-web"},
                    points=_oscillating(12, 8),
                )
            ],
        )

    if "server_api_delay_bucket" in lowered or "histogram_quantile" in lowered:
        return MetricResult(
            query=expr,
            source="sample",
            series=[
                MetricSeries(labels={"app": "server-feed"}, points=_ramp(0.4, 3.2)),
                MetricSeries(labels={"app": "server-a4api-default"}, points=_ramp(0.35, 0.42)),
            ],
        )

    if "nginx_ingress_controller_requests" in lowered:
        # Impact queries: error requests per second, then total, so a rate can be derived.
        if "5.." in expr:
            return _scoped(MetricResult(
                query=expr,
                source="sample",
                series=[
                    MetricSeries(labels={"host": "www.newsbreak.com"}, points=_ramp(0.4, 128.0)),
                    MetricSeries(labels={"host": "api.newsbreak.com"}, points=_ramp(0.2, 1.1)),
                ],
            ))
        return _scoped(MetricResult(
            query=expr,
            source="sample",
            series=[
                MetricSeries(labels={"host": "www.newsbreak.com"}, points=_ramp(720.0, 690.0)),
                MetricSeries(labels={"host": "api.newsbreak.com"}, points=_ramp(4100.0, 4050.0)),
            ],
        ))

    if "5.." in expr or "5xx" in lowered:
        return MetricResult(
            query=expr,
            source="sample",
            series=[
                MetricSeries(labels={"host": "www.newsbreak.com"}, points=_ramp(0.001, 0.18)),
                MetricSeries(labels={"host": "api.newsbreak.com"}, points=_ramp(0.001, 0.004)),
            ],
        )

    if "feed_empty_total" in lowered:
        return MetricResult(
            query=expr,
            source="sample",
            series=[
                MetricSeries(labels={"channel_id": ""}, points=_ramp(5, 320)),
                MetricSeries(labels={"channel_id": "k26164"}, points=_ramp(8, 12)),
            ],
        )

    if "get_empty_docids" in lowered:
        return MetricResult(
            query=expr,
            source="sample",
            series=[MetricSeries(labels={"host": "prod-fe"}, points=_ramp(10, 140))],
        )

    return MetricResult(query=expr, source="sample", series=[])
