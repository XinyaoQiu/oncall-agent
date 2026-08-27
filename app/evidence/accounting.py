"""Four numbers about a set of observations, because one number lies.

"12 queries issued" reads as "we looked and it is fine". Queries that returned data, queries
that came back empty and queries that failed are three different stories — one about the
system, one about the tooling, and one about nothing at all — and an empty result is
indistinguishable from a healthy one unless it is counted separately and said out loud.
"""

from collections.abc import Iterable

from app.evidence.envelope import Observation


def metric_accounting(observations: Iterable[Observation]) -> dict[str, int]:
    """Queries issued, queries with data, queries that returned nothing, queries that failed."""
    items = list(observations)
    failed = [o for o in items if o.error]
    answered = [o for o in items if not o.error]
    with_data = [o for o in answered if not o.is_empty]
    return {
        "queries": len(items),
        "with_data": len(with_data),
        "empty": len(answered) - len(with_data),
        "failed": len(failed),
        "series": sum(len(o.series) for o in answered),
    }
