"""The triage pipeline, from Slack text to a diagnosis."""

from .alerts import get_alert
from .config import Settings
from .llm import LLMClient
from .models import ThreadMessage, TriageResult
from .sources.grafana import GrafanaClient
from .sources.knowledge import KnowledgeRepo
from .analysis import diagnose, evidence, identify, thread


def _knowledge_terms(alert_name: str, labels: dict[str, str]) -> list[str]:
    known = get_alert(alert_name)
    terms = list(known.knowledge_terms) if known else []
    for key in ("app", "deployment", "service", "host"):
        if value := labels.get(key):
            terms.append(value)
    return terms


def triage(
    text: str,
    messages: list[ThreadMessage] | None = None,
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
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
        hits = repo.search_many(_knowledge_terms(identity.alert_name, identity.labels))
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
    result.diagnosis = diagnose.diagnose(
        llm, identity, rule, deployment, metrics, hits, benign, priors
    )
    return result


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

    if result.metrics:
        queried = [m for m in result.metrics if not m.error]
        lines += ["", f"_Queried {len(queried)} metric series"]
        if result.knowledge_hits:
            lines[-1] += f", {len(result.knowledge_hits)} knowledge hits"
        lines[-1] += "_"

    return "\n".join(lines)
