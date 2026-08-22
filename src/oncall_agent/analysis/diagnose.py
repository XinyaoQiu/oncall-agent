"""Diagnosis: the model's judgment over the gathered evidence."""

from datetime import datetime

from ..config import Deployment
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
    deployment: Deployment | None,
    metrics: list[MetricResult],
    knowledge: list[KnowledgeHit],
    benign: list[str],
    priors: list[str],
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

    if deployment:
        sections.append(
            f"\n# Deployment\n"
            f"app: {deployment.app_label}\n"
            f"pods: {deployment.pod_pattern} (~{deployment.replicas} replicas)\n"
            f"traffic: {deployment.traffic_share}\n"
            f"routes: {', '.join(deployment.code_route_paths)}"
        )

    if metrics:
        if any(m.source == "sample" for m in metrics):
            sections.append(
                "\n# Metrics — SAMPLE DATA, NOT THIS SYSTEM\n"
                "No metrics backend is configured, so these numbers are fixtures. They "
                "describe nothing about the live system. Do not cite them as evidence, "
                "and cap your confidence at low."
            )
        else:
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

    sections.append(
        "\n# Task\nDiagnose. Cite evidence for every claim. State whether the alerting "
        "service is the cause or a victim of something upstream."
    )
    return "\n".join(sections)


def diagnose(
    llm: LLMClient,
    identity: AlertIdentity,
    rule: AlertRule | None,
    deployment: Deployment | None,
    metrics: list[MetricResult],
    knowledge: list[KnowledgeHit],
    benign: list[str],
    priors: list[str],
) -> Diagnosis:
    prompt = build_prompt(identity, rule, deployment, metrics, knowledge, benign, priors)
    result = llm.generate_json(prompt, _DIAGNOSIS_SCHEMA, deep=True, system=SYSTEM_PROMPT)

    return Diagnosis(
        summary=result["summary"],
        likely_cause=result["likely_cause"],
        confidence=Confidence(result.get("confidence", "low")),
        victim_or_cause=result["victim_or_cause"],
        evidence_cited=result.get("evidence_cited", []),
        suggested_next_steps=result.get("suggested_next_steps", []),
        related_incidents=result.get("related_incidents", []),
    )
