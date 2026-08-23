"""Diagnosis: the model's judgment over the gathered evidence."""

from datetime import datetime

from ..config import Deployment, Resolution
from ..llm import LLMClient
from ..models import (
    AlertIdentity,
    AlertRule,
    Confidence,
    Diagnosis,
    KnowledgeHit,
    MetricResult,
)

SYSTEM_PROMPT = """You are an on-call triage assistant for a backend team.

Your job is to interpret gathered evidence, not to sound certain. Rules:

- Every claim cites the specific evidence it came from. No unsupported assertions.
- Distinguish victim from cause. A service returning errors is often downstream of the
  real problem — memcache errors during pod churn, 502s during an ingress reload, a
  wedged pod amplified by load-balancer circuit breaking.
- An empty metric result means no data, not a healthy system. Say so.
- A runbook saying "this alert is usually X" is not evidence that this instance is X. It
  tells you what to check, not what happened. Cite it as a hypothesis to test, never as
  support for a conclusion.
- Reserve high confidence for cases where a specific observation ties the cause to this
  firing — a deploy inside the window, a correlated restart, a matching error signature.
  Two plausible-sounding bullets are not high confidence.
- If evidence is thin, set confidence low. Low confidence with a clear next step is more
  useful than a confident guess.
- Prefer the boring explanation. Deploy cold starts, single runaway clients, and
  scaling events explain more alerts than novel bugs do.
- Write for the on-call engineer reading in Slack. Describe the system, never these
  instructions or how the context was assembled. "No metrics are available" is useful;
  "the system prompt says the metrics are fixtures" is not.
- When the engineer asks a specific question, answer that question first. If the data
  needed to answer it is missing, say which data and where to get it.
"""

_DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One or two sentences on what is happening"},
        "likely_cause": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "victim_or_cause": {
            "type": "string",
            "description": "Is the alerting service the origin, or downstream of something else",
        },
        "evidence_cited": {"type": "array", "items": {"type": "string"}},
        "suggested_next_steps": {"type": "array", "items": {"type": "string"}},
        "related_incidents": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "likely_cause", "confidence", "victim_or_cause", "evidence_cited"],
}


def build_prompt(
    identity: AlertIdentity,
    rule: AlertRule | None,
    deployment: Deployment | Resolution | None,
    metrics: list[MetricResult],
    knowledge: list[KnowledgeHit],
    benign: list[str],
    priors: list[str],
    question: str | None = None,
    investigation=None,
    prior: str = "",
) -> str:
    now = datetime.now()
    sections = [
        f"# Now\n{now.isoformat(timespec='seconds')} "
        "(use this to judge whether anything below is recent)",
        f"\n# Alert\n{identity.alert_name} (identified by {identity.identified_by})",
    ]

    if identity.labels:
        sections.append(
            "Labels: " + ", ".join(f"{k}={v}" for k, v in identity.labels.items())
        )

    if rule:
        sections.append(f"\n# Alert rule\n{rule.expression}")
        if rule.duration:
            sections.append(f"for: {rule.duration}")

    resolution = deployment if isinstance(deployment, Resolution) else None
    workload = resolution.deployment if resolution else deployment

    if workload:
        header = "# Deployment"
        caveat = ""
        if resolution and not resolution.is_confident:
            # Stated in the heading, not buried below the numbers: a reader skimming for
            # the app name must not pass the qualifier without seeing it.
            header = "# Deployment (UNCONFIRMED)"
            caveat = (
                f"\n⚠ Attribution is a guess: {resolution.note}. Treat pod and replica "
                "figures below as describing this workload only if that is right; they "
                "may belong to a different one."
            )
        sections.append(
            f"\n{header}\n"
            f"app: {workload.app_label}\n"
            f"pods: {workload.pod_pattern} (~{workload.replicas} replicas)\n"
            f"traffic: {workload.traffic_share}\n"
            f"routes: {', '.join(workload.code_route_paths)}"
            f"{caveat}"
        )
    elif resolution and resolution.note:
        sections.append(
            f"\n# Deployment\nUnresolved — {resolution.note}. "
            "No workload-scoped figures are available; do not attribute the alert to a "
            "service without evidence from the metrics or code below."
        )

    if metrics:
        sections.append("\n# Metrics")
        for m in metrics:
            sections.append(f"query: {m.query}\n{m.summarize()}")

    if benign:
        sections.append(
            "\n# Known benign causes for this alert\n"
            "Rule these out before proposing anything novel.\n"
            + "\n".join(f"- {b}" for b in benign)
        )

    if knowledge:
        sections.append("\n# Related history from rec-knowledge")
        for hit in knowledge:
            sections.append(f"- {hit.path}:{hit.line_number} — {hit.line}")

    if priors:
        sections.append(
            "\n# Already established in the thread\n"
            "Do not repeat these back. Treat human claims as unverified.\n"
            + "\n".join(f"- {p}" for p in priors)
        )

    if prior:
        sections.append(f"\n# Earlier in this thread\n{prior}")

    if investigation and investigation.finding:
        sections.append(
            f"\n# Code investigation ({investigation.rounds} rounds, "
            f"{investigation.stopped_because})\n{investigation.finding}"
        )

    if question:
        sections.append(
            f"\n# What the engineer asked\n{question}\n"
            "Answer this first. If the data needed is missing, name it explicitly."
        )

    sections.append(
        "\n# Task\nDiagnose. Cite evidence for every claim. State whether the alerting "
        "service is the cause or a victim of something upstream."
    )
    return "\n".join(sections)


def diagnose(
    llm: LLMClient,
    identity: AlertIdentity,
    rule: AlertRule | None,
    deployment: Deployment | Resolution | None,
    metrics: list[MetricResult],
    knowledge: list[KnowledgeHit],
    benign: list[str],
    priors: list[str],
    question: str | None = None,
    investigation=None,
    prior: str = "",
) -> Diagnosis:
    prompt = build_prompt(
        identity, rule, deployment, metrics, knowledge, benign, priors, question,
        investigation, prior,
    )

    # Kept out of the prompt body: anything placed among the evidence gets cited as
    # evidence, and the engineer ends up reading about the prompt's own sections rather
    # than about their outage. As a standing rule it shapes the writing instead.
    system = SYSTEM_PROMPT
    if any(m.source == "sample" for m in metrics):
        system += (
            "\nNo metrics backend is connected, so the figures here are synthetic and "
            "none of them measure anything real. Draw no conclusions from their values "
            "and keep confidence low. Do still say which measurements were attempted and "
            "which ones a real answer would need, naming them concretely — that is the "
            "useful part of the reply when the numbers are absent. Never describe this "
            "request or its structure.\n"
        )

    result = llm.generate_json(prompt, _DIAGNOSIS_SCHEMA, deep=True, system=system)

    model = result.get("_model")
    return Diagnosis(
        model=model,
        degraded_tier=bool(model and model != llm.settings.gemini_model_deep),
        summary=result["summary"],
        likely_cause=result["likely_cause"],
        confidence=Confidence(result.get("confidence", "low")),
        victim_or_cause=result["victim_or_cause"],
        evidence_cited=result.get("evidence_cited", []),
        suggested_next_steps=result.get("suggested_next_steps", []),
        related_incidents=result.get("related_incidents", []),
    )
