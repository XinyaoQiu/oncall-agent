"""The one renderer. Every adapter goes through it.

Spec §9 item 19: in the old repo each disclosure banner — synthetic data, unconfirmed
attribution, unresolved deployment, degraded model — was computed inside
`pipeline.format_reply()`, the Slack formatter. The moment a second surface (SSE, CLI, a
write-up) needs the same reply it grows a second formatter, the two drift, and the one that
drifts is the one that silently stops disclosing. A missing banner looks exactly like a
clean run.

So banners are computed here, from state alone, and `markup` chooses emphasis characters
and bullet glyphs — nothing else. There is no code path by which a markup choice can change
which banners appear.
"""

from loguru import logger
from pydantic import BaseModel

from app.graph.state import OncallState

_MARKUP = {
    "markdown": {
        "bold": "**",
        "italic": "_",
        "bullet": "-",
        "icons": {"warning": "⚠️", "synthetic": "🧪", "info": "ℹ️"},
    },
    "slack": {
        "bold": "*",
        "italic": "_",
        "bullet": "•",
        "icons": {
            "warning": ":warning:",
            "synthetic": ":test_tube:",
            "info": ":information_source:",
        },
    },
}


class Banner(BaseModel):
    """A disclosure line. `text` is markup-free so no adapter can drop or reword one."""

    level: str
    text: str


class _Style:
    """Markup only. It cannot see state, so it cannot decide what is disclosed."""

    def __init__(self, markup: str) -> None:
        spec = _MARKUP.get(markup)
        if spec is None:
            # Falling back keeps disclosure intact; raising would lose the whole reply.
            logger.warning("unknown markup {!r}, rendering as markdown", markup)
            spec = _MARKUP["markdown"]
        self._bold = spec["bold"]
        self._italic = spec["italic"]
        self._bullet = spec["bullet"]
        self._icons = spec["icons"]

    def bold(self, text: str) -> str:
        return f"{self._bold}{text}{self._bold}"

    def italic(self, text: str) -> str:
        return f"{self._italic}{text}{self._italic}"

    def item(self, text: str) -> str:
        return f"{self._bullet} {text}"

    def banner(self, banner: Banner) -> str:
        icon = self._icons.get(banner.level, self._icons["warning"])
        body = self.italic(banner.text) if banner.level == "info" else self.bold(banner.text)
        return f"{icon} {body}"


def banners(state: OncallState) -> list[Banner]:
    """Every disclosure the reader cannot derive by looking at the numbers.

    Each of these failures renders identically to a good run: fixture data has plausible
    values, a guessed workload's pod query returns true data about the wrong service, and a
    weaker model writes with the same confidence as the strong one.
    """
    out: list[Banner] = []

    if state.get("used_synthetic"):
        out.append(
            Banner(
                level="synthetic",
                text=(
                    "Sample metrics — generated fixture data, not measurements. "
                    "Nothing below reflects the live system."
                ),
            )
        )

    resolution = state.get("resolution")
    if resolution is not None:
        note = f" — {resolution.note}" if resolution.note else ""
        if resolution.confidence == "low" and resolution.app_label:
            out.append(
                Banner(
                    level="warning",
                    text=(
                        f"Service attribution unconfirmed{note}. "
                        f"Workload figures below assume {resolution.app_label}."
                    ),
                )
            )
        elif resolution.confidence == "unresolved" and resolution.note:
            out.append(
                Banner(
                    level="warning",
                    text=(
                        f"No deployment resolved{note}. "
                        "Nothing below is scoped to a workload."
                    ),
                )
            )

    degraded = state.get("degraded_model")
    if degraded:
        out.append(
            Banner(
                level="warning",
                text=(
                    f"Answered by {degraded} — the usual model was overloaded. "
                    "Analysis may be shallower than normal."
                ),
            )
        )

    identity = state.get("identity")
    if identity is not None and identity.alert_name == "unknown":
        out.append(
            Banner(
                level="info",
                text="Could not identify this alert. Tell me the alert name and I'll retry.",
            )
        )

    return out


def accounting(state: OncallState) -> str:
    """Queries issued, series returned, queries that came back empty, queries that failed.

    Four different numbers. Reporting only the first reads as "we looked and it is fine"
    when in fact nothing came back — and an empty result and a failed one are different
    stories, one about the system and one about the tooling.

    Skipped probes are part of the same accounting: a probe that did not run says so, in
    words. Silence is what makes a thin pack read like a complete one.
    """
    observations = list(state.get("baseline") or [])
    failed = [o for o in observations if o.error]
    empty = [o for o in observations if not o.error and o.is_empty]
    series = sum(len(o.series) for o in observations if not o.error)

    lines = [
        f"{len(observations)} queries issued, {series} series returned, "
        f"{len(empty)} empty, {len(failed)} failed"
    ]
    for probe in state.get("skipped") or []:
        lines.append(f"not measured: {probe.probe} — {probe.reason}")
    return "\n".join(lines)


def render_answer(state: OncallState, *, markup: str = "markdown") -> str:
    """Compose the whole reply. `markup` selects glyphs, never content."""
    style = _Style(markup)
    identity = state.get("identity")
    lines = [style.bold(identity.alert_name if identity else "unknown")]

    for banner in banners(state):
        lines.append(style.banner(banner))

    diagnosis = state.get("diagnosis")
    if diagnosis is not None:
        lines += [
            "",
            diagnosis.summary,
            "",
            style.bold(f"Likely cause ({diagnosis.confidence} confidence)"),
            diagnosis.likely_cause,
            "",
            f"{style.bold('Victim or cause:')} {diagnosis.victim_or_cause}",
        ]
        if diagnosis.evidence_cited:
            lines += ["", style.bold("Based on")]
            lines += [style.item(e) for e in diagnosis.evidence_cited]
        if diagnosis.suggested_next_steps:
            lines += ["", style.bold("Next steps")]
            lines += [style.item(s) for s in diagnosis.suggested_next_steps]
        if diagnosis.related_incidents:
            lines += ["", style.bold("Related history")]
            lines += [style.item(r) for r in diagnosis.related_incidents]
    else:
        lines += ["", style.italic("No cause determined — below is only what was measured.")]

    steps = list(state.get("past_steps") or [])
    if steps:
        lines += ["", style.bold("Investigation trail")]
        for step in steps:
            outcome = step.result if step.ok else f"failed: {step.result}"
            lines.append(style.item(f"{step.step} — {outcome}"))

    stopped = state.get("stopped_because")
    if stopped:
        lines.append(style.italic(f"stopped early: {stopped}"))

    lines += [""] + [style.italic(line) for line in accounting(state).splitlines()]
    return "\n".join(lines)
