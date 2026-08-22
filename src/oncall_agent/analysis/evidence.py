"""Deterministic evidence gathering: metrics, deployment resolution, benign checks.

No model calls here. This half of the work is measurement, not judgment.
"""

from ..alerts import KnownAlert, get_alert
from ..config import Deployment, resolve_deployment
from ..models import AlertIdentity, AlertRule, MetricResult
from ..sources.grafana import GrafanaClient, GrafanaUnavailable


def fetch_rule(grafana: GrafanaClient, identity: AlertIdentity) -> AlertRule | None:
    """Recover the authoritative expression, falling back to the registry."""
    try:
        rule = grafana.fetch_rule(identity.alert_name)
    except GrafanaUnavailable:
        rule = None

    if rule:
        return rule

    known = get_alert(identity.alert_name)
    if known and known.fallback_expr:
        return AlertRule(
            name=identity.alert_name,
            expression=known.fallback_expr,
            annotations={"note": "expression from local registry; Grafana rule not found"},
        )
    return None


def resolve_service(identity: AlertIdentity, rule: AlertRule | None) -> Deployment | None:
    """Map host/path to a deployment. A lookup, not a search."""
    host = identity.labels.get("host")
    path = identity.labels.get("path")
    app = identity.labels.get("app") or identity.labels.get("deployment")

    if not (host or path) and rule:
        # The rule expression usually carries the path selector even when the rendered
        # Slack text dropped it.
        for key in ("path", "host"):
            marker = f'{key}="'
            if marker in rule.expression:
                value = rule.expression.split(marker, 1)[1].split('"', 1)[0]
                if key == "path":
                    path = value
                else:
                    host = value

    return resolve_deployment(host=host, path=path, app_label=app)


def gather(
    grafana: GrafanaClient,
    identity: AlertIdentity,
    rule: AlertRule | None,
    deployment: Deployment | None,
    *,
    minutes: int = 60,
) -> list[MetricResult]:
    """Collect the metric evidence.

    The alert's own expression is queried first, at its own granularity. Broadening the
    view before reproducing the alert is how a host-scoped problem gets waved away as
    healthy by a site-wide average.
    """
    results: list[MetricResult] = []

    if rule and rule.expression:
        results.append(grafana.query_range(rule.expression, minutes=minutes))

    if deployment:
        results.append(grafana.replica_count(deployment.app_label, minutes=minutes))
        results.append(grafana.pod_starts(deployment.app_label, minutes=minutes))

    return results


def benign_checks(identity: AlertIdentity) -> list[str]:
    """The cheap explanations worth ruling out first."""
    known: KnownAlert | None = get_alert(identity.alert_name)
    if not known:
        return []
    return [f"{p.name}: {p.description} (check: {p.how_to_check})" for p in known.benign_patterns]
