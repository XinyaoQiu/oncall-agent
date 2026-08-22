"""Grafana / Mimir access: alert rule lookup and PromQL queries."""

import time

import httpx

from ..config import Settings
from ..models import AlertRule, MetricResult, MetricSeries
from . import sample_metrics


class GrafanaUnavailable(RuntimeError):
    pass


class GrafanaClient:
    """Reads the authoritative rule definition, then reproduces it.

    The Slack message identifies the alert; it never supplies the numbers. Rendered
    alert text rounds values and drops labels, so treating it as data would introduce a
    second, untrusted measurement of something the metrics store already holds.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.use_sample = settings.use_sample_metrics or not settings.grafana_url

        if not self.use_sample:
            self._client = httpx.Client(
                base_url=settings.grafana_url.rstrip("/"),
                headers={"Authorization": f"Bearer {settings.grafana_token}"},
                timeout=30.0,
            )

    def fetch_rule(self, alert_name: str) -> AlertRule | None:
        """Look up the rule definition to recover the real PromQL expression."""
        if self.use_sample:
            return sample_metrics.rule_for(alert_name)

        try:
            resp = self._client.get("/api/v1/rules")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise GrafanaUnavailable(f"rule lookup failed: {exc}") from exc

        for group in resp.json().get("data", {}).get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("name", "").lower() == alert_name.lower():
                    return AlertRule(
                        name=rule["name"],
                        expression=rule.get("query", ""),
                        duration=rule.get("duration"),
                        labels=rule.get("labels", {}),
                        annotations=rule.get("annotations", {}),
                    )
        return None

    def query_range(
        self, expr: str, *, minutes: int = 60, step: str = "1m", end: float | None = None
    ) -> MetricResult:
        """Run a range query. Failures are reported, never silently emptied."""
        if self.use_sample:
            return sample_metrics.query(expr, minutes=minutes)

        end_ts = end or time.time()
        try:
            resp = self._client.get(
                "/api/v1/query_range",
                params={
                    "query": expr,
                    "start": end_ts - minutes * 60,
                    "end": end_ts,
                    "step": step,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return MetricResult(query=expr, error=str(exc))

        payload = resp.json()
        if payload.get("status") != "success":
            return MetricResult(query=expr, error=payload.get("error", "unknown error"))

        series = [
            MetricSeries(
                labels=item.get("metric", {}),
                points=[(float(ts), float(val)) for ts, val in item.get("values", [])],
            )
            for item in payload.get("data", {}).get("result", [])
        ]
        return MetricResult(query=expr, series=series)

    def pod_starts(self, app_label: str, minutes: int = 60) -> MetricResult:
        """Pod start times — the cheap check for deploy-warmup alerts."""
        expr = f'kube_pod_start_time{{pod=~"{app_label}-.*"}}'
        return self.query_range(expr, minutes=minutes)

    def replica_count(self, app_label: str, minutes: int = 60) -> MetricResult:
        """Replica count over time reveals HPA oscillation."""
        expr = f'kube_deployment_status_replicas{{deployment="{app_label}"}}'
        return self.query_range(expr, minutes=minutes)
