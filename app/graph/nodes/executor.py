"""Run one plan step against the merged toolset.

Three defects in the reference implementation are fixed here rather than inherited, because
each one is silent — the run completes, the reply is fluent, and the missing part is
invisible:

1. **Steps could not compose.** The reference builds its messages from the current step
   alone ("避免原始任务干扰"), so step 3 cannot use what step 2 found. A plan whose steps
   "build on the previous one" — its own planner prompt says exactly that — then executes as
   three unrelated queries. Previous results are carried in.
2. **`.content` was assumed to be a string.** With a reasoning model or any provider that
   returns content blocks it is a list, and `len(result)` counts blocks while the step's
   text is dropped on the floor. `_text()` flattens both shapes.
3. **Tool calls in the second response were dropped.** After the tool results go back, the
   model very often wants one more call; the reference reads `.content` off that reply and
   discards the calls, so the step reports on a lookup it never performed. Here the exchange
   loops until the model stops asking, bounded by `MAX_TOOL_ROUNDS`.

The two guards — duplicate calls and missing required arguments — are applied *before* the
tool runs and answered with a `ToolMessage` naming what was wrong, so the model can correct
itself in the same step instead of burning a round on the same broken call.
"""

import time
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from loguru import logger

from app.config import Settings
from app.core import ToolLoader, llm_factory
from app.domain.sources import contract_for
from app.evidence.envelope import ExecutedStep
from app.graph.guards import (
    SIGNATURE_SEP,
    call_signature,
    duplicate_message,
    missing_required_args,
    recorded_signatures,
    rejection_message,
)
from app.graph.state import OncallState
from app.tools.registry import result_is_synthetic

MAX_OBSERVATION_CHARS = 2500
MAX_RESULT_CHARS = 4000
MAX_TOOL_ROUNDS = 3

SYSTEM_PROMPT = """You execute one step of an on-call investigation using the tools provided.

- Do the step that is asked, using the earlier results shown to you where they help.
- Use the tool the step names. If a call is rejected, read the rejection: it says exactly
  what was missing or why the call was pointless, and reissuing it unchanged will fail again.
- Report only what the tools returned. Every result carries its own caveats — sampling rate,
  retention, "not usable for" — and those caveats belong in what you report, not stripped out.
- An empty result is a finding: say the query returned nothing. It is not the same as
  saying the system is healthy.
- If a tool fails, say which one and how. Do not substitute a plausible number for a
  measurement you could not take.
- Answer for this step only. Do not summarise the incident or guess at a cause.
"""


def _progress(stage: str, message: str, **payload: Any) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:
        return
    if writer:
        writer({"stage": stage, "message": message, **payload})


def _text(message: Any) -> str:
    """The text of a model reply, whether `content` is a string or a list of blocks."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                value = block.get("text") or block.get("content") or block.get("reasoning")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(p for p in parts if p.strip())
    return "" if content is None else str(content)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def step_context(state: OncallState, task: str) -> str:
    """What this step needs to know: the floor, and what the earlier steps returned."""
    sections = []

    observations = list(state.get("baseline") or [])
    if observations:
        sections.append(
            "# Evidence already collected\n"
            + _clip("\n\n".join(o.render() for o in observations), MAX_OBSERVATION_CHARS * 2)
        )

    past = list(state.get("past_steps") or [])
    if past:
        sections.append(
            "# Earlier steps in this investigation, and what they returned\n"
            + "\n\n".join(
                f"## {s.step}\n{_clip(s.result, MAX_OBSERVATION_CHARS)}" for s in past
            )
        )

    remaining = list(state.get("plan") or [])[1:]
    if remaining:
        sections.append(
            "# Steps that come after this one (do not do them now)\n"
            + "\n".join(f"- {s}" for s in remaining)
        )

    sections.append(f"# Your step\n{task}")
    return "\n\n".join(sections)


async def _run_call(
    state: OncallState,
    by_name: dict[str, BaseTool],
    call: dict[str, Any],
    seen: set[str],
) -> tuple[str, str | None, bool]:
    """Execute one tool call, or reject it by name. Returns (text, signature, synthetic)."""
    name = str(call.get("name") or "")
    args = call.get("args") or {}
    tool = by_name.get(name)

    if tool is None:
        return f"Call rejected: there is no tool named {name!r}.", None, False

    missing = missing_required_args(tool, args)
    if missing:
        return rejection_message(name, missing, args), None, False

    signature = call_signature(name, args)
    if signature in seen or signature in recorded_signatures(state):
        return duplicate_message(name, args), None, False

    seen.add(signature)
    try:
        raw = await tool.ainvoke(args)
    except Exception as exc:
        logger.warning(f"tool {name} failed: {exc}")
        return f"{name} failed: {type(exc).__name__}: {exc}", signature, False

    rendered = _clip(str(raw), MAX_OBSERVATION_CHARS)
    synthetic = contract_for(name).synthetic or result_is_synthetic(rendered)
    return rendered, signature, synthetic


def executor_node(settings: Settings, load: ToolLoader) -> Callable[[OncallState], Any]:
    async def executor(state: OncallState) -> dict[str, Any]:
        plan = list(state.get("plan") or [])
        if not plan:
            logger.info("executor: nothing to execute")
            return {}

        task = plan[0]
        started = time.perf_counter()
        _progress("executor", task, step=task)

        tools, _ = await load()
        by_name = {t.name: t for t in tools}

        try:
            llm = llm_factory.get_llm(settings, deep=False)
        except Exception as exc:
            logger.error(f"executor: model unavailable: {exc}")
            return {
                "plan": plan[1:],
                "past_steps": [
                    ExecutedStep(step=task, result=f"not run — model unavailable: {exc}", ok=False)
                ],
            }

        bound = llm.bind_tools(tools) if tools else llm
        messages: list[Any] = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=step_context(state, task)),
        ]

        seen: set[str] = set()
        signatures: list[str] = []
        synthetic = False
        result = ""

        for round_no in range(MAX_TOOL_ROUNDS + 1):
            try:
                response = await bound.ainvoke(messages)
            except Exception as exc:
                logger.error(f"executor: step failed: {exc}")
                return {
                    "plan": plan[1:],
                    "past_steps": [
                        ExecutedStep(
                            step=task,
                            result=f"step failed: {type(exc).__name__}: {exc}",
                            tool=SIGNATURE_SEP.join(signatures) or None,
                            ok=False,
                            elapsed_ms=int((time.perf_counter() - started) * 1000),
                        )
                    ],
                    **({"used_synthetic": True} if synthetic else {}),
                }

            messages.append(response)
            calls = list(getattr(response, "tool_calls", None) or [])
            if not calls:
                result = _text(response)
                break

            if round_no == MAX_TOOL_ROUNDS:
                # The calls are not dropped silently: the step says it ran out of rounds.
                messages.append(
                    HumanMessage(
                        content=(
                            f"Tool budget for this step is spent after {MAX_TOOL_ROUNDS} rounds. "
                            "Report what the calls so far returned; do not request more."
                        )
                    )
                )
                try:
                    result = _text(await llm.ainvoke(messages))
                except Exception as exc:
                    result = f"step ran out of tool rounds and could not be summarised: {exc}"
                break

            for call in calls:
                text, signature, was_synthetic = await _run_call(state, by_name, call, seen)
                synthetic = synthetic or was_synthetic
                if signature:
                    signatures.append(signature)
                messages.append(
                    ToolMessage(content=text, tool_call_id=str(call.get("id") or signature or ""))
                )

        if not result.strip():
            last_text = next(
                (_text(m) for m in reversed(messages) if isinstance(m, (AIMessage, ToolMessage))),
                "",
            )
            result = last_text or "the step produced no output"

        update: dict[str, Any] = {
            "plan": plan[1:],
            "past_steps": [
                ExecutedStep(
                    step=task,
                    result=_clip(result, MAX_RESULT_CHARS),
                    tool=SIGNATURE_SEP.join(signatures) or None,
                    ok=True,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            ],
        }
        if synthetic:
            update["used_synthetic"] = True

        logger.info(f"executor: {task[:80]!r} → {len(signatures)} tool call(s)")
        return update

    return executor
