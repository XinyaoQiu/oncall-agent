"""Deterministic evidence gathering: metrics, deployment resolution, benign checks.

No model calls here. This half of the work is measurement, not judgment.
"""

from ..alerts import KnownAlert, get_alert
from ..config import Deployment, Resolution, resolve
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


def resolve_service(identity: AlertIdentity, rule: AlertRule | None) -> Resolution:
    """Map host/path to a deployment. A lookup, not a search.

    Returns the resolution rather than the deployment: a guess that came from the
    catch-all has to travel with that fact attached, or downstream queries about the
    wrong workload look exactly like queries about the right one.
    """
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

    return resolve(host=host, path=path, app_label=app)


def gather(
    grafana: GrafanaClient,
    identity: AlertIdentity,
    rule: AlertRule | None,
    resolution: Resolution | Deployment | None,
    *,
    minutes: int = 60,
) -> list[MetricResult]:
    """Collect the metric evidence.

    The alert's own expression is queried first, at its own granularity. Broadening the
    view before reproducing the alert is how a host-scoped problem gets waved away as
    healthy by a site-wide average.
    """
    resolution = _as_resolution(resolution)
    deployment = resolution.deployment
    results: list[MetricResult] = []

    if rule and rule.expression:
        results.append(grafana.query_range(rule.expression, minutes=minutes))

    if deployment:
        # Still queried when the resolution is a guess: dropping the probe would leave
        # the pack emptier without making it more honest. The caveat rides along instead.
        caveat = "" if resolution.is_confident else f"unconfirmed workload — {resolution.note}"
        for result in (
            grafana.replica_count(deployment.app_label, minutes=minutes),
            grafana.pod_starts(deployment.app_label, minutes=minutes),
        ):
            result.caveat = caveat
            results.append(result)
    elif app := identity.labels.get("app") or identity.labels.get("service"):
        # No deployment in the table, but the alert named a workload. Query it directly
        # rather than returning nothing: §3.5's label-free path.
        for result in (
            grafana.replica_count(app, minutes=minutes),
            grafana.pod_starts(app, minutes=minutes),
        ):
            result.caveat = f"{app!r} is not in the deployment table; queried as named"
            results.append(result)

    results.extend(quantify_impact(grafana, identity, deployment, minutes=minutes))
    return results


def _as_resolution(value: Resolution | Deployment | None) -> Resolution:
    """Accept either form, so callers holding a bare Deployment keep working."""
    if isinstance(value, Resolution):
        return value
    if value is None:
        return Resolution()
    return Resolution(deployment=value, confidence="exact", matched_by="caller")


def quantify_impact(
    grafana: GrafanaClient,
    identity: AlertIdentity,
    deployment: Deployment | None,
    *,
    minutes: int = 60,
) -> list[MetricResult]:
    """How many requests and hosts this is actually affecting.

    Counts come from the ingress layer. Application logs are sampled — roughly 1 in 8 —
    so counting there understates impact by nearly an order of magnitude. That mistake
    is expensive precisely because the resulting number looks authoritative in an
    incident review, so the source is fixed here rather than left to the caller.
    """
    hosts = identity.labels.get("host") or (
        "|".join(deployment.hosts) if deployment else None
    )
    if not hosts:
        return []

    selector = f'host=~"{hosts}"'
    return [
        grafana.query_range(
            f'sum(rate(nginx_ingress_controller_requests{{{selector},status=~"5.."}}[5m])) by (host)',
            minutes=minutes,
        ),
        grafana.query_range(
            f"sum(rate(nginx_ingress_controller_requests{{{selector}}}[5m])) by (host)",
            minutes=minutes,
        ),
    ]


def benign_checks(identity: AlertIdentity) -> list[str]:
    """The cheap explanations worth ruling out first."""
    known: KnownAlert | None = get_alert(identity.alert_name)
    if not known:
        return []
    return [f"{p.name}: {p.description} (check: {p.how_to_check})" for p in known.benign_patterns]
