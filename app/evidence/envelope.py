"""The provenance envelope: a measurement and what it is worth, in one object.

Every number the model sees arrives wrapped in one of these. The caveat renders in the same
string as the value, so a reader — human or model — cannot take the number without it.

Two renderings are deliberate rather than cosmetic:

- An empty result says so in words. A missing series and a healthy service produce the same
  empty list, and a Loki shard fault, a retention boundary and a wrong index all produce it
  too. "no data" is a measurement outcome; "healthy" is a conclusion.
- A metric whose *value* is a point in time renders as an age. `kube_pod_start_time` returns
  an epoch; a max over epochs is meaningless, and a large number reads as "recent" to
  anything that does not know better.
"""

import time

from pydantic import BaseModel, Field

from app.domain.sources import SourceContract, contract_for


def relative(seconds: float) -> str:
    """Render an age the way a person would say it."""
    seconds = abs(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} hours ago"
    return f"{seconds / 86400:.1f} days ago"


class Series(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)
    points: list[tuple[float, float]] = Field(default_factory=list)


class Observation(BaseModel):
    """One measurement, with its query, its source contract, and its caveats."""

    query: str
    purpose: str = ""
    series: list[Series] = Field(default_factory=list)
    text: str | None = None
    source: str = "unknown"
    error: str | None = None
    caveats: list[str] = Field(default_factory=list)

    @property
    def contract(self) -> SourceContract:
        return contract_for(self.source)

    @property
    def is_empty(self) -> bool:
        return not self.series and not self.text

    @property
    def is_timestamp_metric(self) -> bool:
        return "_time" in self.query or "_timestamp" in self.query

    def all_caveats(self) -> list[str]:
        contract_caveat = self.contract.caveat()
        return [c for c in ([*self.caveats, contract_caveat]) if c]

    def render(self, limit: int = 5, *, now: float | None = None) -> str:
        """Compact text for a prompt or a Slack reply. Caveats come first."""
        head = f"query: {self.query}"
        if self.purpose:
            head += f"   [{self.purpose}]"
        lines = [head]
        lines += [f"  ⚠ {c}" for c in self.all_caveats()]

        if self.error:
            lines.append(f"  query failed: {self.error}")
            return "\n".join(lines)

        if self.text is not None:
            lines.append("  " + self.text.replace("\n", "\n  "))
            return "\n".join(lines)

        if not self.series:
            lines.append("  no data returned (this is not the same as 'healthy')")
            return "\n".join(lines)

        reference = now if now is not None else time.time()
        for s in self.series[:limit]:
            label_str = ", ".join(f"{k}={v}" for k, v in s.labels.items())
            lines.append(f"  {{{label_str}}} {self._describe(s, reference)}")
        if len(self.series) > limit:
            lines.append(f"  ... and {len(self.series) - limit} more series")
        return "\n".join(lines)

    def _describe(self, series: Series, now: float) -> str:
        values = [v for _, v in series.points]
        if not values:
            return "(no points)"
        if self.is_timestamp_metric:
            return relative(now - values[-1])
        low, high, last = min(values), max(values), values[-1]
        if high == low:
            return f"steady at {high:.4g}"
        return f"min={low:.4g} max={high:.4g} latest={last:.4g}"


class SkippedProbe(BaseModel):
    """A measurement that was not taken, and why.

    Silence is what makes a thin pack read like a complete one. "No host label, so impact
    was not quantified" is a useful line; a missing section is not.
    """

    probe: str
    reason: str


class ExecutedStep(BaseModel):
    """One plan step and what it returned."""

    step: str
    result: str
    tool: str | None = None
    ok: bool = True
    elapsed_ms: int = 0
