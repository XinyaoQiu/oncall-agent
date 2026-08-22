"""Stage 1: figure out which alert a Slack message is about.

Rules first, model as fallback. Identification is allowed to be approximate because a
wrong guess fails loudly at rule lookup — unlike a wrong metric value, which fails
silently and reaches the incident review as fact.
"""

import re
from datetime import datetime

from ..alerts import KNOWN_ALERTS, match_alert
from ..llm import LLMClient, LLMUnavailable
from ..models import AlertIdentity, Confidence

_LABEL_RE = re.compile(r'(\w+)\s*[=:]\s*"?([\w./\-]+)"?')

_IDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "alert_name": {"type": "string", "enum": [a.name for a in KNOWN_ALERTS] + ["unknown"]},
        "labels": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "path": {"type": "string"},
                "app": {"type": "string"},
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["alert_name", "reasoning"],
}


def extract_labels(text: str) -> dict[str, str]:
    """Pull label-ish key/value pairs out of the rendered alert text."""
    interesting = {"host", "path", "app", "service", "channel_id", "cluster", "deployment"}
    return {k: v for k, v in _LABEL_RE.findall(text) if k.lower() in interesting}


def identify(text: str, llm: LLMClient | None = None) -> AlertIdentity:
    labels = extract_labels(text)

    matched = match_alert(text)
    if matched:
        return AlertIdentity(
            alert_name=matched.name,
            labels=labels,
            fired_at=datetime.now(),
            confidence=Confidence.HIGH,
            identified_by="rules",
        )

    if llm is None:
        return AlertIdentity(
            alert_name="unknown",
            labels=labels,
            confidence=Confidence.LOW,
            identified_by="rules",
        )

    prompt = (
        "Identify which alert this Slack message is about. Return 'unknown' if it does "
        "not clearly match one of the known alerts.\n\n"
        f"Known alerts:\n"
        + "\n".join(f"- {a.name}: matches {', '.join(a.keywords)}" for a in KNOWN_ALERTS)
        + f"\n\nSlack message:\n{text[:2000]}"
    )

    try:
        result = llm.generate_json(prompt, _IDENTIFY_SCHEMA)
    except LLMUnavailable:
        return AlertIdentity(
            alert_name="unknown", labels=labels, confidence=Confidence.LOW, identified_by="rules"
        )

    name = result.get("alert_name", "unknown")
    return AlertIdentity(
        alert_name=name,
        labels={**labels, **result.get("labels", {})},
        fired_at=datetime.now(),
        confidence=Confidence.LOW if name == "unknown" else Confidence.MEDIUM,
        identified_by="llm",
    )
