"""Data models for the triage pipeline."""

import time
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ThreadMessage(BaseModel):
    """One message in a Slack thread."""

    user: str
    text: str
    ts: str
    is_bot: bool = False


class AlertIdentity(BaseModel):
    """Stage 1 output: which alert this is.

    Deliberately approximate. A wrong identification fails loudly at rule lookup,
    unlike a wrong metric value which fails silently.
    """

    alert_name: str
    labels: dict[str, str] = Field(default_factory=dict)
    fired_at: datetime | None = None
    confidence: Confidence = Confidence.MEDIUM
    identified_by: str = "rules"


class AlertRule(BaseModel):
    """Stage 2 output: the authoritative rule definition from Grafana."""

    name: str
    expression: str
    duration: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)


class MetricSeries(BaseModel):
    labels: dict[str, str]
    points: list[tuple[float, float]]

    def peak(self) -> float | None:
        return max((v for _, v in self.points), default=None)


def _relative(seconds: float) -> str:
    """Render an age the way a person would say it."""
    seconds = abs(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} hours ago"
    return f"{seconds / 86400:.1f} days ago"


class MetricResult(BaseModel):
    """A metric query result, always carrying the query that produced it."""

    query: str
    series: list[MetricSeries] = Field(default_factory=list)
    source: str = "grafana"
    error: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.series

    @property
    def is_timestamp_metric(self) -> bool:
        """Metrics whose *value* is a point in time, not a magnitude.

        A peak over these is meaningless, and a raw epoch number invites the reader to
        assume it is recent.
        """
        return "_time" in self.query or "_timestamp" in self.query

    def summarize(self, limit: int = 5, *, now: float | None = None) -> str:
        """Compact text form for the prompt."""
        if self.error:
            return f"query failed: {self.error}"
        if self.is_empty:
            # An empty result is not evidence of health.
            return "no data returned (this is not the same as 'healthy')"

        reference = now if now is not None else time.time()
        lines = []
        for s in self.series[:limit]:
            label_str = ", ".join(f"{k}={v}" for k, v in s.labels.items())
            lines.append(f"  {{{label_str}}} {self._describe(s, reference)}")

        if len(self.series) > limit:
            lines.append(f"  ... and {len(self.series) - limit} more series")
        return "\n".join(lines)

    def _describe(self, series: MetricSeries, now: float) -> str:
        values = [v for _, v in series.points]
        if not values:
            return "(no points)"

        if self.is_timestamp_metric:
            # The value IS a time. Say how long ago, not how large.
            return _relative(now - values[-1])

        low, high, last = min(values), max(values), values[-1]
        if high == low:
            return f"steady at {high:.4g}"
        return f"min={low:.4g} max={high:.4g} latest={last:.4g}"


class KnowledgeHit(BaseModel):
    """A match from rec-knowledge."""

    path: str
    line_number: int
    line: str
    matched_term: str
    context: str = ""


class Diagnosis(BaseModel):
    """The model's analysis. Every claim must cite what it came from."""

    summary: str
    likely_cause: str
    confidence: Confidence
    victim_or_cause: str = Field(
        description="Whether the alerting service is the origin or a downstream victim"
    )
    evidence_cited: list[str] = Field(default_factory=list)
    suggested_next_steps: list[str] = Field(default_factory=list)
    related_incidents: list[str] = Field(default_factory=list)
    model: str | None = None
    degraded_tier: bool = False


class InvestigationSummary(BaseModel):
    """What the search loop established, if it ran."""

    rounds: int = 0
    finding: str | None = None
    stopped_because: str = ""
    tools_used: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    """Everything one invocation produced."""

    identity: AlertIdentity
    rule: AlertRule | None = None
    metrics: list[MetricResult] = Field(default_factory=list)
    knowledge_hits: list[KnowledgeHit] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None
    thread_priors: list[str] = Field(default_factory=list)
    investigation: InvestigationSummary | None = None


class KnowledgeEntry(BaseModel):
    """A candidate write-back to rec-knowledge."""

    title: str
    filename: str
    body: str
    services: list[str] = Field(default_factory=list)
    supersedes: str | None = None
