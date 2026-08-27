"""The deterministic floor: what gets measured, every time, before anyone reasons.

Spec §9 constraint 7, and the reason this package exists. `collect_baseline` takes the
alert's identity, its rule and the deployment resolution — and nothing else. It cannot see
the engineer's question, the thread, what a human already ruled out, or a list of "extra
probes" a planner would like run.

**The signature is the enforcement.** Not a prompt, not a review comment: there is no
parameter through which a prior belief could reach this function, so "CPU is fine, skip it"
is inexpressible rather than merely discouraged. A human saying CPU is fine may have looked
at the wrong dashboard, or looked before the spike, and the pack's whole value is that it
was gathered independently. Prior information reorders work downstream; it never removes it
here. A test asserts the parameter names, so widening it fails CI.

Two orderings are load-bearing:

- The alert's own query is observation 0 (§9 constraint 3). Reproducing the alert at its own
  label granularity before any broader view is what stops a host-scoped failure from being
  refuted by a site-wide average that looks flat.
- Everything that cannot run leaves a `SkippedProbe` saying why. Silence is what makes a
  thin pack read like a complete one.
"""

import asyncio

from loguru import logger

from app.domain.models import AlertIdentity, AlertRule, Resolution
from app.evidence.envelope import Observation, SkippedProbe
from app.evidence.impact import NO_BACKEND, MetricQuery, quantify_impact, run_metric_query
from app.evidence.rules import provenance_caveat

ALERT_QUERY_PROBE = "the alert's own query"
WORKLOAD_PROBE = "deployment history (replicas, pod starts)"

NO_RULE_REASON = (
    "no rule expression was recovered or synthesized, so the alert's own measurement "
    "could not be reproduced; everything below is broader than the alert"
)
NO_WORKLOAD_REASON = (
    "no deployment resolved and the alert named no app or service, so there is no "
    "workload to ask about replicas or restarts"
)


def _workload(
    identity: AlertIdentity | None, resolution: Resolution | None
) -> tuple[str | None, str | None, str]:
    """The workload to query, its pod pattern, and the caveat that must ride with both."""
    if resolution is not None and resolution.app_label:
        if resolution.is_confident:
            caveat = ""
        else:
            # Still queried: dropping the probe leaves the pack emptier without making it
            # more honest. The standing of the guess rides along instead.
            detail = resolution.note or f"matched by {resolution.matched_by}"
            caveat = f"unconfirmed workload — {detail}"
        return resolution.app_label, resolution.pod_pattern, caveat

    labels = identity.labels if identity else {}
    named = labels.get("app") or labels.get("service") or labels.get("deployment")
    if named:
        return named, None, f"{named!r} is not in the deployment table; queried as named"
    return None, None, ""


def _impact_host(identity: AlertIdentity | None, resolution: Resolution | None) -> str | None:
    labels = identity.labels if identity else {}
    host = labels.get("host")
    if host:
        return host
    if resolution is not None and resolution.hosts:
        return "|".join(resolution.hosts)
    return None


async def _alert_own_query(
    rule: AlertRule | None, minutes: int, query_metric: MetricQuery | None
) -> tuple[list[Observation], list[SkippedProbe]]:
    if rule is None or not rule.expression:
        return [], [SkippedProbe(probe=ALERT_QUERY_PROBE, reason=NO_RULE_REASON)]
    if query_metric is None:
        return [], [SkippedProbe(probe=ALERT_QUERY_PROBE, reason=NO_BACKEND)]

    observation = await run_metric_query(
        query_metric,
        rule.expression,
        purpose=f"the alert's own expression at its own granularity ({rule.provenance})",
        minutes=minutes,
        caveats=[provenance_caveat(rule)],
    )
    return [observation], []


async def _workload_probes(
    identity: AlertIdentity | None,
    resolution: Resolution | None,
    minutes: int,
    query_metric: MetricQuery | None,
) -> tuple[list[Observation], list[SkippedProbe]]:
    app_label, pod_pattern, caveat = _workload(identity, resolution)
    if not app_label:
        return [], [SkippedProbe(probe=WORKLOAD_PROBE, reason=NO_WORKLOAD_REASON)]
    if query_metric is None:
        return [], [SkippedProbe(probe=WORKLOAD_PROBE, reason=NO_BACKEND)]

    pods = pod_pattern or f"{app_label}-.*"
    observations = await asyncio.gather(
        run_metric_query(
            query_metric,
            f'kube_deployment_status_replicas{{deployment="{app_label}"}}',
            purpose="replica count over the window (HPA oscillation, capacity change)",
            minutes=minutes,
            caveats=[caveat],
        ),
        run_metric_query(
            query_metric,
            f'kube_pod_start_time{{pod=~"{pods}"}}',
            purpose="pod start times (deploy cold start, crash looping)",
            minutes=minutes,
            caveats=[caveat],
        ),
    )
    return list(observations), []


async def collect_baseline(
    identity: AlertIdentity | None,
    rule: AlertRule | None,
    resolution: Resolution | None,
    *,
    minutes: int = 60,
    query_metric: MetricQuery | None = None,
) -> tuple[list[Observation], list[SkippedProbe]]:
    """Measure the alert, then its workload, then its impact. Always in that order.

    Returns the observations and the probes that did not run, with reasons. It raises
    nothing: a backend that is down produces failed observations and skips, because the run
    that would have disclosed the outage must not be the one the outage stops.
    """
    observations, skipped = await _alert_own_query(rule, minutes, query_metric)

    workload, impact = await asyncio.gather(
        _workload_probes(identity, resolution, minutes, query_metric),
        quantify_impact(
            _impact_host(identity, resolution), minutes=minutes, query_metric=query_metric
        ),
    )
    for probe_observations, probe_skips in (workload, impact):
        observations.extend(probe_observations)
        skipped.extend(probe_skips)

    logger.info(f"baseline: {len(observations)} observations, {len(skipped)} probes skipped")
    return observations, skipped
