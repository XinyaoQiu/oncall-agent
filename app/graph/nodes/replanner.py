"""Continue, re-plan, or answer — with the limits taken out of the model's hands.

The reference implementation writes its limits into the prompt: *"新步骤数量必须 <= 当前剩余
步骤数"*, *"总步骤数已执行 >= 5 次时，禁止 replan"*, *"已执行步骤 >= 5（无论结果如何）"*. Two
of the four are then also checked in Python; the other two are not. A prompt-only limit is
one the model can decline, and the decline leaves no trace — a run that should have stopped
at five steps and stopped at eleven looks like a run that was simply thorough.

Here every limit lives in `guards`, and the prompt below deliberately says nothing about
step counts. The model contributes a judgment about *sufficiency*; the budget is not its
business.

Two failure modes are decided rather than inherited:

- An action outside the vocabulary means `continue`. The plan already exists and is the
  safer default; a typo should not become an early answer.
- A model failure means `respond`. Retrying here spends the budget that the responder needs
  to write up what was already found, and what was already found is the part worth keeping.
"""

from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger
from pydantic import BaseModel, Field

from app.config import Settings
from app.core import ToolLoader, llm_factory
from app.graph.guards import budget_from_config, clamp_replan, replan_banned, should_stop
from app.graph.state import OncallState
from app.tools.registry import describe_tools

ACTIONS = ("continue", "replan", "respond")
MAX_STEP_CHARS = 800

SYSTEM_PROMPT = """You decide what an on-call investigation does next.

Choose one action:

- `respond` — what has been gathered already answers the question. Prefer this. An answer
  that says "here is what we measured and here is what it does not settle" is useful; a
  perfect answer that arrives after the incident is over is not.
- `continue` — the remaining steps are necessary and will produce something the answer
  needs. Use this when a specific remaining step is load-bearing.
- `replan` — the remaining steps are aimed at the wrong thing, given what came back. Supply
  replacement steps.

Judge sufficiency only. Do not reason about how many steps have run or how much time is
left; that is decided elsewhere and is not your concern.

A step that returned nothing is information, not a reason to retry it. A step that failed
for a tooling reason may be worth replacing with a different route to the same fact.
"""


class Act(BaseModel):
    """The decision. `action` is a plain string so an unexpected value is handled here
    rather than raised as a validation error and mistaken for a model outage."""

    action: str = Field(default="continue", description="one of: continue, replan, respond")
    new_steps: list[str] = Field(
        default_factory=list, description="replacement steps; only read when action is 'replan'"
    )
    reason: str = Field(default="", description="one sentence on why")


def _progress(stage: str, message: str, **payload: Any) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:
        return
    if writer:
        writer({"stage": stage, "message": message, **payload})


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _history(state: OncallState) -> str:
    steps = list(state.get("past_steps") or [])
    if not steps:
        return "(nothing has been executed yet)"
    return "\n\n".join(
        f"## {s.step}\n{'' if s.ok else 'FAILED: '}{_clip(s.result, MAX_STEP_CHARS)}"
        for s in steps
    )


def replanner_node(settings: Settings, load: ToolLoader) -> Callable[..., Any]:
    async def replanner(state: OncallState, config: RunnableConfig | None = None) -> dict[str, Any]:
        budget = budget_from_config(settings, config)
        plan = list(state.get("plan") or [])

        stop = should_stop(state, budget)
        if stop:
            logger.info(f"replanner: stopping — {stop}")
            _progress("replanner", f"stopping: {stop}")
            return {"plan": [], "stopped_because": stop}

        if not plan:
            logger.info("replanner: plan exhausted, answering")
            return {}

        tools, _ = await load()
        try:
            llm = llm_factory.get_llm(settings, deep=False)
            act = await llm.with_structured_output(Act).ainvoke(
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(
                        content=(
                            f"# The question\n{state.get('input') or '(an alert, no question)'}\n\n"
                            f"# Tools available\n{describe_tools(tools)}\n\n"
                            f"# What has been executed\n{_history(state)}\n\n"
                            "# Steps still planned\n" + "\n".join(f"- {s}" for s in plan)
                        )
                    ),
                ]
            )
        except Exception as exc:
            # Answering from partial findings beats spending the remaining budget on retries.
            logger.error(f"replanner: model unavailable, answering from what we have: {exc}")
            return {
                "plan": [],
                "stopped_because": f"re-planning stopped — the model was unavailable ({exc})",
            }

        if isinstance(act, Act):
            action, new_steps = act.action, act.new_steps
        else:
            payload = act or {}
            action, new_steps = payload.get("action", "continue"), payload.get("new_steps", [])

        action = str(action or "").strip().lower()
        if action not in ACTIONS:
            logger.warning(f"replanner: unknown action {action!r}; continuing the current plan")
            action = "continue"

        if action == "respond":
            logger.info("replanner: enough evidence, answering")
            return {"plan": []}

        if action == "replan":
            ban = replan_banned(state, budget)
            if ban:
                logger.info(f"replanner: {ban}")
                return {"plan": [], "stopped_because": ban}

            clamped = clamp_replan(new_steps, plan)
            if not clamped:
                logger.warning("replanner: replan with no usable steps; keeping the current plan")
                return {}
            logger.info(f"replanner: replanned to {len(clamped)} step(s)")
            _progress("replanner", f"replanned to {len(clamped)} steps", plan=clamped)
            return {"plan": clamped}

        logger.info(f"replanner: continuing, {len(plan)} step(s) left")
        return {}

    return replanner


def route_after_replan(state: OncallState) -> str:
    """Structural: a plan with steps left goes back to the executor, anything else answers."""
    return "executor" if state.get("plan") else "respond"
