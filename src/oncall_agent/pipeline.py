"""The triage pipeline, from Slack text to a diagnosis."""

from .alerts import get_alert
from .config import Settings
from .llm import LLMClient
from .models import InvestigationSummary, ThreadMessage, TriageResult
from .repos import default_registry
from .sources.grafana import GrafanaClient
from .sources.knowledge import KnowledgeRepo
from .analysis import diagnose, evidence, identify, thread
from .investigate.loop import investigate
from .investigate.tools import build_toolset


def _knowledge_terms(
    alert_name: str, labels: dict[str, str], deployment=None
) -> list[str]:
    known = get_alert(alert_name)
    terms = list(known.knowledge_terms) if known else []

    for key in ("app", "deployment", "service", "host"):
        if value := labels.get(key):
            terms.append(value)

    # The deployment is often resolved from the rule expression rather than from the
    # rendered alert labels, so searching labels alone misses history about the very
    # service that is alerting.
    if deployment:
        terms.append(deployment.app_label)
        terms.extend(deployment.hosts)

    seen, unique = set(), []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            unique.append(term)
    return unique


def triage(
    text: str,
    messages: list[ThreadMessage] | None = None,
    *,
    question: str | None = None,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    deep: bool = True,
    on_step=None,
) -> TriageResult:
    """Run the full pipeline.

    Raises LLMUnavailable if the model cannot be reached — the agent declines rather
    than replying with an evidence dump that reads like a complete analysis.
    """
    settings = settings or Settings.from_env()
    llm = llm or LLMClient(settings)
    messages = messages or []

    identity = identify.identify(text, llm)
    priors = thread.extract_priors(messages, llm)

    grafana = GrafanaClient(settings)
    rule = evidence.fetch_rule(grafana, identity)
    deployment = evidence.resolve_service(identity, rule)
    metrics = evidence.gather(
        grafana, identity, rule, deployment, minutes=settings.query_window_minutes
    )
    benign = evidence.benign_checks(identity)

    hits = []
    try:
        repo = KnowledgeRepo(settings.knowledge_repo)
        repo.pull()
        hits = repo.search_many(
            _knowledge_terms(identity.alert_name, identity.labels, deployment)
        )
    except Exception:
        # Knowledge is context, not a gate. Its absence weakens the diagnosis but the
        # model is told what it has, so it can lower confidence accordingly.
        pass

    result = TriageResult(
        identity=identity,
        rule=rule,
        metrics=metrics,
        knowledge_hits=hits,
        thread_priors=priors,
    )

    if deep and settings.investigation_rounds > 0:
        result.investigation = _investigate(
            llm, settings, grafana, identity, rule, deployment, question, on_step
        )

    result.diagnosis = diagnose.diagnose(
        llm, identity, rule, deployment, metrics, hits, benign, priors, question,
        investigation=result.investigation,
    )
    return result


def _investigate(
    llm, settings, grafana, identity, rule, deployment, question, on_step
) -> InvestigationSummary | None:
    """Run the search loop, letting the diagnosis proceed if it fails.

    The loop is the part that can follow a lead the first pass did not anticipate, but
    it is also the part most likely to run long or error — so a failure here degrades
    the answer rather than losing the evidence already gathered.
    """
    registry = default_registry(settings.repo_root)
    if not registry.available():
        return None

    lines = [f"Alert {identity.alert_name} firing."]
    if identity.labels:
        lines.append("Labels: " + ", ".join(f"{k}={v}" for k, v in identity.labels.items()))
    if rule:
        lines.append(f"Rule: {rule.expression}")
    if deployment:
        lines.append(f"Served by {deployment.app_label} ({', '.join(deployment.hosts)}).")
    if question:
        lines.append(f"The engineer asked: {question}")

    try:
        inv = investigate(
            llm, build_toolset(settings, registry, grafana), registry, "\n".join(lines),
            max_rounds=settings.investigation_rounds,
            wall_clock_seconds=settings.investigation_seconds,
            on_step=on_step,
        )
    except Exception:
        return None

    return InvestigationSummary(
        rounds=inv.rounds,
        finding=inv.finding,
        stopped_because=inv.stopped_because,
        tools_used=[s.tool for s in inv.steps],
    )


def format_reply(result: TriageResult) -> str:
    """Render for Slack. Evidence and judgment stay visibly separate."""
    d = result.diagnosis
    lines = [f"*{result.identity.alert_name}*"]

    if result.identity.alert_name == "unknown":
        lines.append("_Could not identify this alert. Tell me the alert name and I'll retry._")

    if d:
        lines += [
            "",
            d.summary,
            "",
            f"*Likely cause* ({d.confidence.value} confidence)",
            d.likely_cause,
            "",
            f"*Victim or cause:* {d.victim_or_cause}",
        ]

        if d.evidence_cited:
            lines += ["", "*Based on*"] + [f"• {e}" for e in d.evidence_cited]

        if d.suggested_next_steps:
            lines += ["", "*Next steps*"] + [f"• {s}" for s in d.suggested_next_steps]

        if d.related_incidents:
            lines += ["", "*Related history*"] + [f"• {r}" for r in d.related_incidents]

    inv = result.investigation
    if inv and inv.finding:
        lines += [
            "",
            f"*Code investigation* ({inv.rounds} rounds: {' → '.join(inv.tools_used)})",
            inv.finding,
        ]

    if d and d.degraded_tier:
        lines.insert(
            1,
            f":warning: _Answered by {d.model} — the usual model was overloaded. "
            "Analysis may be shallower than normal._",
        )

    if any(m.source == "sample" for m in result.metrics):
        # The reader cannot tell fixtures from live data by looking at the numbers, so
        # say it where they will see it rather than trusting the model to mention it.
        lines.insert(
            1,
            ":test_tube: *Sample metrics — no metrics backend configured.* "
            "Nothing below reflects the live system.",
        )

    if result.metrics:
        # Queries issued, queries that returned data, and series found are three
        # different numbers. Reporting only the first reads as "we looked and it's
        # fine" when in fact nothing came back.
        with_data = [m for m in result.metrics if m.series]
        failed = [m for m in result.metrics if m.error]

        parts = [f"{len(result.metrics)} queries"]
        parts.append(f"{sum(len(m.series) for m in with_data)} series")
        if len(with_data) < len(result.metrics) - len(failed):
            parts.append(f"{len(result.metrics) - len(failed) - len(with_data)} empty")
        if failed:
            parts.append(f"{len(failed)} failed")
        if result.knowledge_hits:
            parts.append(f"{len(result.knowledge_hits)} knowledge hits")

        lines += ["", f"_{', '.join(parts)}_"]

    return "\n".join(lines)
