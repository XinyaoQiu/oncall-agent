"""What to look at next, given what the floor already measured.

The planner is the first node that sees a human's words, and the first that may be wrong in
an interesting way. Two things keep that bounded, and neither is in the prompt: the baseline
has already run (so a plan cannot decide *not* to measure), and the plan's length is
truncated by `guards`, so "and then eleven more probes" is not a thing the model can produce.

Retrieval runs before planning, as in the reference implementation, but its result is
labelled: a runbook says what to check, not what happened. That distinction is in
`config/sources.yaml` as `not_usable_for: [measurement]` and it renders with every hit.
"""

from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

from app.config import Settings
from app.core import ToolLoader, llm_factory
from app.graph.state import OncallState
from app.tools.registry import describe_tools

KNOWLEDGE_TOOL = "retrieve_knowledge"
MAX_BASELINE_CHARS = 6000
MAX_KNOWLEDGE_CHARS = 4000

SYSTEM_PROMPT = """You plan the investigation of a production alert for a backend on-call team.

A deterministic evidence floor has already been collected and is shown to you. Your plan
covers what to check *next*; do not re-plan measurements that were already taken.

Rules:
- Each step names the tool it needs and the arguments that tool needs. A step nobody can
  execute is a wasted round.
- Steps run in order and each may use what the previous ones returned. Say so when a step
  depends on an earlier result.
- Rule out the cheap benign explanations listed before believing in a novel bug. Deploy
  cold starts, a single runaway client, and scaling events explain more alerts than bugs do.
- An empty measurement is a result, not a healthy system. Planning a step to re-run it
  identically will return the same emptiness.
- Identifiers (error codes, metric names, function names) are exact tokens: search for them
  as they appear in code, not for a description of them.
- Prefer few, decisive steps. Stop planning when the next step would only add confidence to
  something already established.
"""


class Plan(BaseModel):
    """The steps to execute, in order."""

    steps: list[str] = Field(
        default_factory=list,
        description="Ordered, individually executable investigation steps. Each names its tool.",
    )


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


async def retrieve(tools: list[BaseTool], query: str) -> str:
    """Past incidents and runbooks resembling this one. Absent RAG is not an error."""
    tool = next((t for t in tools if t.name == KNOWLEDGE_TOOL), None)
    if tool is None or not query.strip():
        return ""
    try:
        hit = await tool.ainvoke({"query": query})
    except Exception as exc:
        logger.warning(f"knowledge retrieval failed: {exc}")
        return ""
    return _clip(str(hit).strip(), MAX_KNOWLEDGE_CHARS)


def evidence_digest(state: OncallState) -> str:
    """The floor, rendered. Caveats travel inside each observation's own text."""
    observations = list(state.get("baseline") or [])
    if not observations:
        return "(no observations were collected)"
    return _clip("\n\n".join(o.render() for o in observations), MAX_BASELINE_CHARS)


def _context(state: OncallState, tools: list[BaseTool], knowledge: str) -> str:
    identity = state.get("identity")
    rule = state.get("rule")
    resolution = state.get("resolution")

    sections = [f"# Tools you may plan around\n{describe_tools(tools)}"]

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

    sections.append(f"\n# Evidence already collected\n{evidence_digest(state)}")

    skipped = list(state.get("skipped") or [])
    if skipped:
        sections.append(
            "\n# Not measured, and why\n"
            + "\n".join(f"- {s.probe}: {s.reason}" for s in skipped)
        )

    benign = list(state.get("benign") or [])
    if benign:
        sections.append(
            "\n# Cheap benign explanations to rule out first\n"
            + "\n".join(f"- {b}" for b in benign)
        )

    priors = list(state.get("priors") or [])
    if priors:
        sections.append(
            "\n# What the thread already said\n"
            + "\n".join(f"- {p}" for p in priors)
            + "\nThese reorder the work. They do not remove a measurement already taken."
        )

    if knowledge:
        sections.append(
            "\n# Related runbooks and past incidents\n"
            f"{knowledge}\n"
            "These say what to check, never what happened here."
        )
    return "\n".join(sections)


def planner_node(settings: Settings, load: ToolLoader) -> Callable[[OncallState], Any]:
    async def planner(state: OncallState) -> dict[str, Any]:
        question = state.get("input") or state.get("alert_text") or ""
        _progress("planner", "retrieving related incidents")

        tools, _ = await load()
        knowledge = await retrieve(tools, question)

        _progress("planner", "building an investigation plan")
        try:
            llm = llm_factory.get_llm(settings, deep=False)
            result = await llm.with_structured_output(Plan).ainvoke(
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=_context(state, tools, knowledge)),
                    HumanMessage(
                        content=f"The engineer asked: {question or '(nothing — this is an alert)'}"
                    ),
                ]
            )
        except Exception as exc:
            # No plan is a real outcome: the floor was measured and the reply says what it
            # found. Inventing a plan here would only spend the executor's budget on it.
            logger.error(f"planner: model unavailable: {exc}")
            return {
                "plan": [],
                "stopped_because": f"no plan was made — the planning model was unavailable ({exc})",
            }

        steps = result.steps if isinstance(result, Plan) else list((result or {}).get("steps", []))
        steps = [s.strip() for s in steps if isinstance(s, str) and s.strip()][: settings.max_steps]

        logger.info(f"planner: {len(steps)} steps")
        _progress("planner", f"planned {len(steps)} steps", plan=steps)
        return {"plan": steps}

    return planner
