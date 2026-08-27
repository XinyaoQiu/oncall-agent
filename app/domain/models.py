"""Shared domain models.

These live in the domain layer, not in the graph's state module, because the dependency
has to point this way: the graph knows about deployments and alerts, and the fact layer
must not know there is a graph. `app.graph.state` re-exports them so callers that already
import from there keep working.
"""

from typing import Literal

from pydantic import BaseModel, Field

Turn = Literal["triage", "followup", "chat", "writeup", "rating"]


class AlertIdentity(BaseModel):
    """Which alert this is. Deliberately approximate — a wrong guess fails loudly at rule
    lookup, unlike a wrong number, which fails silently."""

    alert_name: str = "unknown"
    labels: dict[str, str] = Field(default_factory=dict)
    identified_by: str = "rules"


class AlertRule(BaseModel):
    """The authoritative rule, and which rung of the §3.1 ladder produced it."""

    name: str
    expression: str
    duration: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    provenance: str = "authoritative"


class Resolution(BaseModel):
    """Which deployment serves this alert, and how sure the lookup is.

    A guess that says it is a guess is usable. The same guess unmarked is a trap: its pod
    and replica queries succeed and return true data about the wrong workload.
    """

    app_label: str | None = None
    pod_pattern: str | None = None
    hosts: list[str] = Field(default_factory=list)
    replicas: int | None = None
    traffic_share: str | None = None
    code_route_paths: list[str] = Field(default_factory=list)
    confidence: Literal["exact", "low", "unresolved"] = "unresolved"
    matched_by: str = "nothing"
    note: str = ""

    @property
    def is_confident(self) -> bool:
        return self.confidence == "exact"


class Diagnosis(BaseModel):
    """The model's judgment. Every claim cites what it came from, and the victim/cause call
    is required because it is the discrimination that past incidents kept getting wrong."""

    summary: str
    likely_cause: str
    confidence: Literal["high", "medium", "low"]
    victim_or_cause: str
    evidence_cited: list[str] = Field(default_factory=list)
    suggested_next_steps: list[str] = Field(default_factory=list)
    related_incidents: list[str] = Field(default_factory=list)
