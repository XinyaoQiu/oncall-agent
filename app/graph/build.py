"""The graph, and the one event stream both adapters read.

```
START ─▶ baseline ─▶ planner ─▶ executor ─▶ replanner ─┬─▶ executor
                                                       └─▶ respond ─▶ END
```

**`baseline` is on the only edge into `planner`** (spec §9 constraint 8). That is the whole
mechanism behind "investigation is unconditional": there is no arc that reaches the planner
without traversing the floor, so a chat turn cannot skip it — with no alert to measure it
produces `SkippedProbe`s instead of observations, which is a different reply, not an absent
one. Any future edge that bypasses `baseline` re-opens the semantic gate §2.3 rejects, so a
test inspects the compiled edges rather than trusting review.

`run_turn` streams `updates` and `custom` together and yields one event shape. Slack's
progress writer and the SSE endpoint then consume the same stream: two renderers of one
event stream drift far less than two event streams, and the one that drifts is the one that
silently stops disclosing.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger

from app.config import Settings
from app.core import ToolLoader
from app.graph.nodes.baseline import baseline_node
from app.graph.nodes.executor import executor_node
from app.graph.nodes.planner import planner_node
from app.graph.nodes.replanner import replanner_node, route_after_replan
from app.graph.nodes.respond import respond_node
from app.graph.state import OncallState
from app.tools.registry import load_tools

BASELINE = "baseline"
PLANNER = "planner"
EXECUTOR = "executor"
REPLANNER = "replanner"
RESPOND = "respond"

_GRAPHS: dict[tuple[int, int], tuple[Settings, Any, Any]] = {}


def tool_loader(settings: Settings) -> ToolLoader:
    """Load the toolset once per graph, not once per node.

    The reference implementation re-dials MCP in the planner, the executor *and* the
    replanner, so a five-step run opens fifteen client sessions and any one of them can fail
    the step it belongs to. One load, one set of warnings, shared.
    """
    cached: dict[str, tuple[list, list[str]]] = {}
    lock = asyncio.Lock()

    async def load() -> tuple[list, list[str]]:
        async with lock:
            if "value" not in cached:
                cached["value"] = await load_tools(
                    settings, include_mcp=bool(settings.mcp_servers)
                )
        return cached["value"]

    return load


def build_graph(settings: Settings, *, checkpointer: Any = None) -> Any:
    """Compile the plan-execute-replan graph. Called once per process, not per turn."""
    from langgraph.graph import END, START, StateGraph

    load = tool_loader(settings)
    graph = StateGraph(OncallState)

    graph.add_node(BASELINE, baseline_node(settings, load))
    graph.add_node(PLANNER, planner_node(settings, load))
    graph.add_node(EXECUTOR, executor_node(settings, load))
    graph.add_node(REPLANNER, replanner_node(settings, load))
    graph.add_node(RESPOND, respond_node(settings))

    graph.add_edge(START, BASELINE)
    graph.add_edge(BASELINE, PLANNER)
    graph.add_edge(PLANNER, EXECUTOR)
    graph.add_edge(EXECUTOR, REPLANNER)
    graph.add_conditional_edges(
        REPLANNER, route_after_replan, {EXECUTOR: EXECUTOR, RESPOND: RESPOND}
    )
    graph.add_edge(RESPOND, END)

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("graph compiled: baseline → planner → executor → replanner → respond")
    return compiled


def graph_for(settings: Settings, checkpointer: Any = None) -> Any:
    """The compiled graph for this settings/checkpointer pair, built at most once.

    Both are kept alive alongside the graph: an id-keyed cache whose keys can be collected
    will happily hand back a graph built for something else that landed on the same address.
    """
    key = (id(settings), id(checkpointer))
    if key not in _GRAPHS:
        _GRAPHS[key] = (settings, checkpointer, build_graph(settings, checkpointer=checkpointer))
    return _GRAPHS[key][2]


def _event(node: str, update: dict[str, Any]) -> dict[str, Any] | None:
    """One node update as an SSE/Slack event. `None` means nothing worth showing."""
    if node == BASELINE:
        identity = update.get("identity")
        return {
            "type": "evidence",
            "stage": BASELINE,
            "message": f"measured the floor for {getattr(identity, 'alert_name', 'unknown')}",
            "alert": getattr(identity, "alert_name", "unknown"),
            "observations": len(update.get("baseline") or []),
            "skipped": len(update.get("skipped") or []),
        }

    if node == PLANNER:
        plan = list(update.get("plan") or [])
        return {
            "type": "plan",
            "stage": PLANNER,
            "message": f"planned {len(plan)} step(s)" if plan else "no plan was made",
            "plan": plan,
        }

    if node == EXECUTOR:
        steps = list(update.get("past_steps") or [])
        if not steps:
            return None
        step = steps[-1]
        return {
            "type": "step",
            "stage": EXECUTOR,
            "message": step.step,
            "step": step.step,
            "ok": step.ok,
            "elapsed_ms": step.elapsed_ms,
        }

    if node == REPLANNER:
        stopped = update.get("stopped_because")
        if stopped:
            return {"type": "status", "stage": REPLANNER, "message": f"stopping: {stopped}"}
        if "plan" in update:
            plan = list(update.get("plan") or [])
            return {
                "type": "plan",
                "stage": REPLANNER,
                "message": f"{len(plan)} step(s) left" if plan else "evidence is sufficient",
                "plan": plan,
            }
        return None

    if node == RESPOND:
        return {
            "type": "answer",
            "stage": RESPOND,
            "message": "wrote up the findings",
            "response": update.get("response") or "",
        }

    return None


async def run_turn(
    state_in: dict[str, Any],
    *,
    settings: Settings,
    thread_id: str,
    checkpointer: Any = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one turn, yielding progress events and finally the answer.

    `started_at` goes into the run config rather than into state: it is a wall-clock stamp
    the guards measure against, and putting it in the config means a resumed turn is
    measured from *this* invocation instead of inheriting the graph's build time.
    """
    graph = graph_for(settings, checkpointer)
    config = {
        "configurable": {"thread_id": thread_id, "started_at": time.time()},
        "recursion_limit": 2 * settings.max_steps + 8,
    }

    yield {"type": "status", "stage": "start", "message": "starting", "thread_id": thread_id}

    response = ""
    try:
        async for mode, chunk in graph.astream(
            state_in, config=config, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                payload = chunk if isinstance(chunk, dict) else {"message": str(chunk)}
                yield {"type": "progress", "stage": payload.get("stage", ""), **payload}
                continue

            for node, update in (chunk or {}).items():
                if not isinstance(update, dict):
                    continue
                if update.get("response"):
                    response = update["response"]
                event = _event(node, update)
                if event:
                    yield event

    except Exception as exc:
        logger.error(f"turn {thread_id} failed: {exc}")
        yield {"type": "error", "stage": "error", "message": f"{type(exc).__name__}: {exc}"}
        return

    yield {
        "type": "complete",
        "stage": "complete",
        "message": "done",
        "response": response,
        "thread_id": thread_id,
    }
