"""The conversational node: one tool-calling pass, not a plan.

A knowledge question does not need a plan. "What does error code 43 mean" wants one
retrieval and an answer, and routing it through plan-execute-replan costs four model calls
where two will do — a difference an engineer feels in Slack.

So chat is a different shape of work, not a degraded triage. What makes that safe is that
the branch into it is decided by structure and intent *before* the graph runs
(app/slack/router.py), never by this node deciding an investigation was unnecessary. A chat
turn in a thread that has an alert is disclosed to the engineer with an offer to triage.
"""

from typing import Any

from loguru import logger

from app.graph.state import OncallState

SYSTEM_PROMPT = """You are an on-call assistant for a backend team, answering in Slack.

You have tools: search code and config across the team's repositories, retrieve runbooks
and past incident write-ups, query metrics, and look up which deployment serves a host or
path. Use them rather than guessing — an identifier is an exact token, so search for the
error code or metric name as it appears in the code.

Rules:
- Answer the question that was asked. Do not turn a definitional question into a diagnosis.
- Cite where an answer came from: a file and line, a runbook, a query.
- Say plainly when you could not find something. "I searched X and Y and found no handler
  for this path" is a useful answer; inventing a plausible file is not.
- An empty query result means no data, not a healthy system.
- Write for a terminal-width Slack message. No preamble."""

MAX_TOOL_ROUNDS = 4


def chat_node(settings, load_tools):
    """Tool-calling with a bounded number of rounds. No planner, no replanner."""

    async def run(state: OncallState) -> dict[str, Any]:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        from app.core.llm_factory import LLMUnavailable, get_llm

        question = (state.get("input") or "").strip()
        digest = state.get("thread_digest") or ""

        try:
            llm = get_llm(settings, deep=False)
        except LLMUnavailable as exc:
            return {
                "response": f":warning: I can't answer right now — {exc}",
                "stopped_because": "model unavailable",
            }

        tools, warnings = await load_tools()
        by_name = {t.name: t for t in tools}
        bound = llm.bind_tools(tools) if tools else llm

        messages: list[Any] = [SystemMessage(content=SYSTEM_PROMPT)]
        if digest:
            messages.append(
                SystemMessage(content=f"Earlier in this Slack thread:\n{digest}")
            )
        messages.append(HumanMessage(content=question or "(no question given)"))

        steps: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            reply = await bound.ainvoke(messages)
            calls = getattr(reply, "tool_calls", None) or []
            if not calls:
                break
            messages.append(reply)
            for call in calls:
                tool = by_name.get(call.get("name", ""))
                if tool is None:
                    observation = f"no such tool: {call.get('name')!r}"
                else:
                    try:
                        observation = str(await tool.ainvoke(call.get("args") or {}))
                    except Exception as exc:
                        observation = f"tool error: {exc}"
                steps.append(f"{call.get('name')}({_brief(call.get('args'))})")
                messages.append(
                    ToolMessage(content=observation, tool_call_id=call.get("id", ""))
                )
        else:
            logger.info(f"chat hit the {MAX_TOOL_ROUNDS}-round ceiling")

        text = _text_of(reply) if isinstance(reply, AIMessage) else str(reply)
        return {
            "response": text or "I could not produce an answer.",
            "stopped_because": "answered",
            "used_synthetic": state.get("used_synthetic", False),
            "past_steps": [],
        }

    return run


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        ).strip()
    return str(content).strip()


def _brief(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    return ", ".join(f"{k}={v!r}"[:40] for k, v in list(args.items())[:3])
