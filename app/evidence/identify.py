"""Which alert this is, decided by rules alone.

Identification is allowed to be approximate: a wrong guess fails loudly at rule lookup,
unlike a wrong metric value, which fails silently and reaches the incident review as fact.
The old implementation had a model fallback here; it is dropped rather than ported, because
the planner reads the raw alert text anyway and a second, worse identifier in front of it
only adds a place for a confident wrong name to be produced.

Links are pulled out here too (tech-design §3.6): a URL in a rendered alert is the alerting
team saying which view shows this failure, and it is the input to the rule ladder's
`reconstructed` rung. They are *parsed*, never fetched.
"""

import re

from app.domain.alerts import match_alert
from app.domain.models import AlertIdentity

LINKS_KEY = "_links"

_LABEL_RE = re.compile(r'(\w+)\s*[=:]\s*"?([\w./\-]+)"?')
_URL_RE = re.compile(r"https?://[^\s<>\"'|)\]}]+")

# Labels that change what gets queried. Everything else in a rendered alert is prose.
_INTERESTING = frozenset(
    {"host", "path", "app", "service", "channel_id", "cluster", "deployment"}
)


def extract_urls(text: str) -> list[str]:
    """Every link in the alert text, de-duplicated, in the order they appear."""
    seen: dict[str, None] = {}
    for match in _URL_RE.findall(text or ""):
        seen.setdefault(match.rstrip(".,;:"), None)
    return list(seen)


def extract_labels(text: str) -> dict[str, str]:
    """Label-ish key/value pairs, plus the alert's links under `_links`.

    The whitelist is deliberate. A rendered alert is mostly prose with numbers in it, and
    every unfiltered `key=value` pair that reaches the resolver is another chance to resolve
    a deployment from something that was never a label.
    """
    labels = {k: v for k, v in _LABEL_RE.findall(text or "") if k.lower() in _INTERESTING}
    links = extract_urls(text)
    if links:
        labels[LINKS_KEY] = ",".join(links)
    return labels


def links_of(identity: AlertIdentity) -> list[str]:
    raw = identity.labels.get(LINKS_KEY, "")
    return [u for u in raw.split(",") if u]


def identify(text: str) -> AlertIdentity:
    """Name the alert from the keyword table, or say `unknown` and carry the labels anyway.

    `unknown` is not a dead end: the labels and links are what the baseline runs on, and per
    spec §9 constraint 8 an unrecognized alert is still investigated.
    """
    matched = match_alert(text or "")
    return AlertIdentity(
        alert_name=matched.name if matched else "unknown",
        labels=extract_labels(text or ""),
        identified_by="rules",
    )
