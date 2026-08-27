"""Known alerts and the cheap benign explanations worth ruling out first.

Each `how_to_check` is the one-line distillation of a real multi-hour investigation, which
is why it lives here as data rather than in a prompt: the model may choose whether to run
the check, but it does not get to invent what the check is.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "alerts.yaml"


class BenignPattern(BaseModel):
    """A recurring benign explanation worth ruling out early."""

    name: str
    description: str
    how_to_check: str


class KnownAlert(BaseModel):
    name: str
    keywords: list[str] = Field(default_factory=list)
    grafana_rule_uid: str | None = None
    fallback_expr: str | None = None
    benign_patterns: list[BenignPattern] = Field(default_factory=list)
    knowledge_terms: list[str] = Field(default_factory=list)


def _load() -> list[KnownAlert]:
    if not CONFIG_PATH.is_file():
        return []
    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return [KnownAlert(**body) for body in (raw.get("alerts") or [])]


KNOWN_ALERTS: list[KnownAlert] = _load()


def match_alert(text: str) -> KnownAlert | None:
    """Rule-based identification. Returns the alert with the most keyword hits."""
    lowered = text.lower()
    best, best_score = None, 0
    for alert in KNOWN_ALERTS:
        score = sum(1 for kw in alert.keywords if kw.lower() in lowered)
        if score > best_score:
            best, best_score = alert, score
    return best


def get_alert(name: str | None) -> KnownAlert | None:
    return next((a for a in KNOWN_ALERTS if a.name == name), None)


def benign_checks(alert_name: str | None) -> list[str]:
    """The cheap explanations worth ruling out before believing the alert."""
    known = get_alert(alert_name)
    if not known:
        return []
    return [
        f"{p.name}: {p.description} (check: {p.how_to_check})" for p in known.benign_patterns
    ]
