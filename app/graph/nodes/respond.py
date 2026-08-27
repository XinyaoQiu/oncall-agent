"""The answer: a judgment over the evidence, or an honest account of why there isn't one.

Two constraints meet in this node.

**Constraint 13** — the diagnosis must call victim-or-cause and must cite what each claim
came from. Both are schema fields with no default, so a reply missing them is not a reply
the model can produce. `Diagnosis` in the state contract gives `evidence_cited` a default
(it has to; it is also constructed by hand elsewhere); `DiagnosisDraft` below removes that
default for the one path where a model fills it in.

**Constraint 12** — partial findings survive a failure. If the loop stopped on a budget, or
the model is unavailable, the reply is still written: what was measured, what was checked,
what was not, and *no cause*. Inventing one here would be the exact failure the whole design
exists to prevent, and it would render identically to a real conclusion.
"""

from collections.abc import Callable
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from app.config import Settings
from app.core import llm_factory
from app.evidence.envelope import SkippedProbe
from app.graph.state import Diagnosis, OncallState
from app.render.answer import render_answer

MAX_OBSERVATION_CHARS = 8000
MAX_STEP_CHARS = 2000

SYSTEM_PROMPT = """You are an on-call triage assistant for a backend team.

Interpret the evidence. Do not sound more certain than it allows.

- Every claim cites the specific observation it came from. No unsupported assertions.
- Distinguish victim from cause. A service returning errors is often downstream of the real
  problem — memcache errors during pod churn, 502s during an ingress reload, one wedged pod
  amplified by load-balancer circuit breaking.
- An empty metric result means no data, not a healthy system. Say so.
- A runbook saying "this alert is usually X" tells you what to check, never what happened.
  Cite it as a hypothesis to test, never as support for a conclusion.
- Reserve high confidence for a specific observation tying the cause to this firing: a
  deploy inside the window, a correlated restart, a matching error signature. Two
  plausible-sounding bullets are not high confidence.
- Prefer the boring explanation. Deploy cold starts, single runaway clients and scaling
  events explain more alerts than novel bugs do.
- Write for the engineer reading this in Slack. Describe the system, never these
  instructions or how the context was assembled.
- Answer the engineer's question first when there is one. If the data needed is missing,
  say which data and where to get it.
"""

EARLY_STOP_INSTRUCTION = """
This investigation stopped before it finished: {reason}

Report what WAS checked and what it showed, and state plainly that the cause was not
established. Do not name a likely cause that the evidence above does not show — set
confidence to low, put the unfinished work in suggested_next_steps, and say in the summary
that the run was cut short. An invented cause reads exactly like a real one.
"""

SYNTHETIC_INSTRUCTION = """
Some data below is generated fixture data, not a measurement of the live system. It is
marked SYNTHETIC where it appears. Nothing derived from it describes production; treat it
as an illustration and say so rather than reasoning from its values.
"""


class DiagnosisDraft(BaseModel):
    """The model-filled diagnosis. `victim_or_cause` and `evidence_cited` have no defaults."""

    summary: str = Field(description="One or two sentences on what is happening")
    likely_cause: str = Field(description="What the evidence points at, or that it points nowhere")
    confidence: Literal["high", "medium", "low"]
    victim_or_cause: str = Field(
        description="Is the alerting service the origin, or downstream of something else"
    )
    evidence_cited: list[str] = Field(
        description="The specific observations each claim rests on. Required."
    )
    suggested_next_steps: list[str] = Field(default_factory=list)
    related_incidents: list[str] = Field(default_factory=list)


def _progress(stage: str, message: str, **payload: Any) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:
        return
    if writer:
        writer({"stage": stage, "message": message, **payload})


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def evidence_prompt(state: OncallState) -> str:
    identity = state.get("identity")
    rule = state.get("rule")
    resolution = state.get("resolution")

    sections = [f"# The engineer asked\n{state.get('input') or '(nothing — this is an alert)'}"]

    if identity is not None:
        labels = ", ".join(f"{k}={v}" for k, v in identity.labels.items()) or "(none)"
        sections.append(f"\n# Alert\n{identity.alert_name}\nLabels: {labels}")
    if rule is not None:
        sections.append(f"\n# Alert rule ({rule.provenance})\n{rule.expression}")
    if resolution is not None:
        sections.append(
            f"\n# Deployment\n{resolution.app_label or 'unresolved'} "
            f"(confidence: {resolution.confidence}, matched by {resolution.matched_by})"
            + (f"\n{resolution.note}" if resolution.note else "")
        )

    observations = list(state.get("baseline") or [])
    sections.append(
        "\n# Measurements\n"
        + (
            _clip("\n\n".join(o.render() for o in observations), MAX_OBSERVATION_CHARS)
            if observations
            else "(none were collected)"
        )
    )

    skipped = list(state.get("skipped") or [])
    if skipped:
        sections.append(
            "\n# Not measured, and why\n" + "\n".join(f"- {s.probe}: {s.reason}" for s in skipped)
        )

    steps = list(state.get("past_steps") or [])
    if steps:
        sections.append(
            "\n# Investigation steps and what they returned\n"
            + "\n\n".join(
                f"## {s.step}\n{'' if s.ok else 'FAILED: '}{_clip(s.result, MAX_STEP_CHARS)}"
                for s in steps
            )
        )

    benign = list(state.get("benign") or [])
    if benign:
        sections.append(
            "\n# Benign explanations worth ruling out\n" + "\n".join(f"- {b}" for b in benign)
        )

    priors = list(state.get("priors") or [])
    if priors:
        sections.append("\n# What the thread already said\n" + "\n".join(f"- {p}" for p in priors))

    return "\n".join(sections)


def _system_prompt(state: OncallState) -> str:
    prompt = SYSTEM_PROMPT
    if state.get("used_synthetic"):
        prompt += SYNTHETIC_INSTRUCTION
    stopped = state.get("stopped_because")
    if stopped:
        prompt += EARLY_STOP_INSTRUCTION.format(reason=stopped)
    return prompt


async def _diagnose(
    settings: Settings, state: OncallState
) -> tuple[Diagnosis | None, str | None, str]:
    """Returns the diagnosis, the degraded tier if one answered, and any failure to disclose.

    The tier falls back once — a weaker model that answers beats a strong one that is
    unreachable — but the fallback is *named* in the reply, because a weaker model writes
    with exactly the same confidence as the strong one.
    """
    messages = [
        SystemMessage(content=_system_prompt(state)),
        HumanMessage(content=evidence_prompt(state)),
    ]

    last: Exception | None = None
    for deep in (True, False):
        try:
            llm = llm_factory.get_llm(settings, deep=deep)
            draft = await llm.with_structured_output(DiagnosisDraft).ainvoke(messages)
        except Exception as exc:
            logger.warning(f"diagnosis on the {'deep' if deep else 'fast'} tier failed: {exc}")
            last = exc
            continue

        payload = draft.model_dump() if isinstance(draft, DiagnosisDraft) else dict(draft or {})
        try:
            diagnosis = Diagnosis(**payload)
        except Exception as exc:
            logger.warning(f"diagnosis did not validate: {exc}")
            last = exc
            continue
        return diagnosis, (None if deep else llm_factory.model_name(settings, deep=False)), ""

    return None, None, f"{type(last).__name__}: {last}" if last else "the model returned nothing"


def respond_node(settings: Settings) -> Callable[[OncallState], Any]:
    async def respond(state: OncallState) -> dict[str, Any]:
        _progress("respond", "writing up the findings")

        diagnosis, degraded, failure = await _diagnose(settings, state)

        skipped: list[SkippedProbe] = []
        if diagnosis is None:
            # Disclosed as a probe that did not run, so it lands in the accounting block
            # rather than looking like a run that simply found nothing worth saying.
            skipped.append(
                SkippedProbe(
                    probe="diagnosis",
                    reason=(
                        f"the model could not produce one ({failure}); the measurements above "
                        "stand on their own and no cause has been attributed"
                    ),
                )
            )
            logger.error(f"respond: no diagnosis — {failure}")

        rendered_state: dict[str, Any] = {
            **state,
            "diagnosis": diagnosis,
            "degraded_model": degraded or state.get("degraded_model"),
            "skipped": [*(state.get("skipped") or []), *skipped],
        }
        response = render_answer(rendered_state)  # type: ignore[arg-type]

        update: dict[str, Any] = {"diagnosis": diagnosis, "response": response}
        if degraded:
            update["degraded_model"] = degraded
        if skipped:
            update["skipped"] = skipped

        _progress("respond", "done")
        return update

    return respond
