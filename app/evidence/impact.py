"""How much traffic is actually affected — counted at the ingress, by construction.

Spec §9 constraint 1. Application logs are sampled at roughly 1/8, so a count taken there
understates impact by nearly an order of magnitude, and the resulting number is expensive
precisely because it looks authoritative in an incident review and nobody re-derives it.

The mechanism is the signature: there is no `source` parameter and there must never be one.
Counting impact from a sampled log is not forbidden by a prompt or a check — it is
inexpressible, because this function has nowhere to put the request.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable
from typing import Any

from app.evidence.envelope import Observation, Series, SkippedProbe

MetricQuery = Callable[..., Awaitable[Any] | Any]

IMPACT_SOURCE = "quantify_impact"
NO_BACKEND = "no metric backend was available to the evidence layer"

NO_HOST_REASON = (
    "the alert carried no host label, and a site-wide request count answers a question "
    "nobody asked — it neither confirms nor bounds this alert's impact"
)


def _series_from(raw: Any) -> list[Series]:
    out: list[Series] = []
    for item in raw:
        if isinstance(item, Series):
            out.append(item)
        elif isinstance(item, dict):
            out.append(Series.model_validate(item))
    return out


def _as_observation(
    expr: str, purpose: str, source: str, caveats: Sequence[str], raw: Any
) -> Observation:
    """Whatever the injected backend returned, as one envelope with its caveats attached."""
    caveat_list = [c for c in caveats if c]

    if isinstance(raw, Observation):
        return raw.model_copy(
            update={
                "purpose": raw.purpose or purpose,
                "source": raw.source if raw.source != "unknown" else source,
                "caveats": [*raw.caveats, *caveat_list],
            }
        )

    if isinstance(raw, dict):
        observation = Observation.model_validate({"query": expr, **raw})
        return observation.model_copy(
            update={
                "purpose": observation.purpose or purpose,
                "source": observation.source if observation.source != "unknown" else source,
                "caveats": [*observation.caveats, *caveat_list],
            }
        )

    if isinstance(raw, (list, tuple)):
        return Observation(
            query=expr,
            purpose=purpose,
            source=source,
            series=_series_from(raw),
            caveats=caveat_list,
        )

    return Observation(
        query=expr,
        purpose=purpose,
        source=source,
        text=raw if isinstance(raw, str) else None,
        caveats=caveat_list,
    )


async def run_metric_query(
    query_metric: MetricQuery,
    expr: str,
    *,
    purpose: str,
    source: str = "query_metric",
    minutes: int = 60,
    caveats: Sequence[str] = (),
) -> Observation:
    """Issue one query. A failure is reported as a failed observation, never as emptiness.

    An exception swallowed into an empty series list is the worst outcome available here:
    empty renders as "no data", and a reader one step removed cannot tell that from healthy.
    """
    try:
        raw = query_metric(expr, minutes=minutes)
        if isawaitable(raw):
            raw = await raw
    except Exception as exc:
        return Observation(
            query=expr,
            purpose=purpose,
            source=source,
            error=f"{type(exc).__name__}: {exc}",
            caveats=[c for c in caveats if c],
        )
    return _as_observation(expr, purpose, source, caveats, raw)


def impact_expressions(host: str) -> tuple[str, str]:
    """The error rate and the total rate, at the ingress, scoped to the alerting host."""
    selector = f'host=~"{host}"'
    return (
        f'sum(rate(nginx_ingress_controller_requests{{{selector},status=~"5.."}}[5m])) by (host)',
        f"sum(rate(nginx_ingress_controller_requests{{{selector}}}[5m])) by (host)",
    )


async def quantify_impact(
    host: str | None, *, minutes: int = 60, query_metric: MetricQuery | None = None
) -> tuple[list[Observation], list[SkippedProbe]]:
    """Requests affected, from the ingress layer. The source is not a parameter (§9.1)."""
    if not host:
        return [], [SkippedProbe(probe="ingress impact", reason=NO_HOST_REASON)]
    if query_metric is None:
        return [], [SkippedProbe(probe="ingress impact", reason=NO_BACKEND)]

    errors_expr, total_expr = impact_expressions(host)
    observations = await asyncio.gather(
        run_metric_query(
            query_metric,
            errors_expr,
            purpose=f"5xx requests at the ingress, last {minutes}m",
            source=IMPACT_SOURCE,
            minutes=minutes,
        ),
        run_metric_query(
            query_metric,
            total_expr,
            purpose=f"total requests at the ingress, last {minutes}m",
            source=IMPACT_SOURCE,
            minutes=minutes,
        ),
    )
    return list(observations), []
