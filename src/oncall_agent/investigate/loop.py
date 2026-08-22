"""The investigation loop.

This is the part that makes the agent an agent: what it does next depends on what the
last round returned. Ruling out cold start is only useful if something then looks
somewhere else, which a fixed pipeline cannot do.

Budgets are enforced here, where the model has no vote. This runs during an incident,
and a thorough answer that arrives after the outage is over is worth nothing.
"""

import time
from dataclasses import dataclass, field

from ..llm import LLMClient, LLMUnavailable
from ..repos import RepoRegistry

MAX_OBSERVATION_CHARS = 2500

# Arguments without which a tool cannot run at all.
REQUIRED_ARGS = {
    "search_code": ("pattern",),
    "read_file": ("repo", "path"),
    "git_log": ("repo",),
    "list_dir": ("repo",),
    "query_metric": ("expr",),
}

_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Why this step, given what the last one returned",
        },
        "tool": {
            "type": "string",
            "enum": [
                "search_code", "read_file", "git_log", "list_dir", "query_metric", "conclude",
            ],
        },
        "pattern": {"type": "string", "description": "search_code: regex or identifier"},
        "repo": {"type": "string"},
        "path": {"type": "string"},
        "path_filter": {"type": "string", "description": "search_code: subdirectory to limit to"},
        "start": {"type": "integer", "description": "read_file: first line"},
        "lines": {"type": "integer", "description": "read_file: how many lines"},
        "since": {"type": "string", "description": "git_log: e.g. '2 days ago'"},
        "expr": {"type": "string", "description": "query_metric: PromQL"},
        "minutes": {"type": "integer"},
        "finding": {"type": "string", "description": "conclude: what was established"},
    },
    "required": ["reasoning", "tool"],
}

SYSTEM_PROMPT = """You are investigating a production alert by searching code and metrics.

Each round you pick one tool. You see its output, then pick the next. Choose based on
what the last round actually returned, not on a plan made before you had results.

- Start where the alert points: the service that owns the alerting endpoint.
- A search that returns nothing is information. Try a different identifier or repo
  rather than the same idea reworded.
- Production spans many repos. A service can break because of a change in a shared
  library, a config repo, or an infra repo — follow the evidence across them.
- Identifiers are exact. Search for the error code, metric name, or function name as it
  appears in the code, not for a description of it.
- Call `conclude` as soon as you can state something specific, and also when you cannot.
  "Searched X and Y, found no handler for this path" is a useful conclusion.
- Do not conclude a cause you have not seen in the output. Reporting that you failed to
  locate it is correct; inventing a plausible file is not.

Tools and their required arguments:
- search_code(pattern, repo?, path_filter?)  — pattern is required; omit repo to search all
- read_file(repo, path, start?, lines?)
- git_log(repo, path?, since?)
- list_dir(repo, path?)
- query_metric(expr, minutes?)               — expr is PromQL
- conclude(finding)
"""


@dataclass
class Step:
    round: int
    tool: str
    reasoning: str
    args: dict
    observation: str
    elapsed: float


@dataclass
class Investigation:
    steps: list[Step] = field(default_factory=list)
    finding: str | None = None
    stopped_because: str = "concluded"

    @property
    def rounds(self) -> int:
        return len(self.steps)

    def transcript(self) -> str:
        if not self.steps:
            return "(no investigation steps)"
        return "\n\n".join(
            f"Round {s.round} — {s.tool}({_format_args(s.args)})\n"
            f"why: {s.reasoning}\n{s.observation}"
            for s in self.steps
        )


def _format_args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items() if v is not None)


def _extract_args(action: dict, tool: str) -> dict:
    """Pull only the arguments this tool accepts.

    The schema is one flat object across all tools, so a response can carry leftovers
    from whichever tool the model considered first.
    """
    keys = {
        "search_code": ("pattern", "repo", "path_filter"),
        "read_file": ("repo", "path", "start", "lines"),
        "git_log": ("repo", "path", "since"),
        "list_dir": ("repo", "path"),
        "query_metric": ("expr", "minutes"),
    }.get(tool, ())
    return {k: action[k] for k in keys if action.get(k) not in (None, "")}


def investigate(
    llm: LLMClient,
    tools: dict,
    registry: RepoRegistry,
    context: str,
    *,
    max_rounds: int = 6,
    wall_clock_seconds: float = 90.0,
    on_step=None,
) -> Investigation:
    """Search until a conclusion, a budget, or nothing left to try.

    `on_step` is called after each round so callers can stream progress; an incident
    channel watching a silent bot for ninety seconds will assume it has hung.
    """
    started = time.monotonic()
    inv = Investigation()
    seen_calls: set[str] = set()

    for round_no in range(1, max_rounds + 1):
        elapsed = time.monotonic() - started
        if elapsed > wall_clock_seconds:
            inv.stopped_because = f"wall-clock budget ({wall_clock_seconds:.0f}s) reached"
            if inv.steps and not inv.finding:
                inv.finding = _summarize_steps(inv)
            break

        prompt = _build_prompt(context, registry, inv, round_no, max_rounds)
        try:
            action = llm.generate_json(prompt, _ACTION_SCHEMA, system=SYSTEM_PROMPT)
        except LLMUnavailable as exc:
            inv.stopped_because = f"model unavailable: {exc}"
            # Rounds already completed found real things. Discarding them because the
            # next call failed throws away work the engineer could still use.
            if inv.steps:
                inv.finding = _summarize_steps(inv)
            break

        tool = action.get("tool", "conclude")
        if tool == "conclude":
            inv.finding = action.get("finding") or action.get("reasoning")
            inv.stopped_because = "concluded"
            break

        fn = tools.get(tool)
        if not fn:
            inv.stopped_because = f"unknown tool: {tool}"
            break

        args = _extract_args(action, tool)

        missing = [p for p in REQUIRED_ARGS.get(tool, ()) if p not in args]
        if missing:
            # Say exactly what was missing. A bare "tool error" leaves the model to
            # guess, and it guesses by reissuing the same broken call.
            observation = (
                f"Call rejected: {tool} requires {', '.join(missing)}, which "
                f"{'was' if len(missing) == 1 else 'were'} not provided. "
                f"Received: {_format_args(args) or '(nothing)'}. "
                "Supply the missing field in your next action."
            )
        elif (signature := f"{tool}({_format_args(args)})") in seen_calls:
            # Repeating a call cannot produce a new observation, so let the round say
            # so rather than spending the budget rediscovering it.
            observation = (
                f"Already ran {signature} this investigation, with the result shown "
                "above. Try a different tool, repo, or search term."
            )
        else:
            seen_calls.add(signature)
            try:
                result = fn(**args)
                observation = result.text
            except Exception as exc:
                observation = f"tool error: {exc}"

        if len(observation) > MAX_OBSERVATION_CHARS:
            observation = observation[:MAX_OBSERVATION_CHARS] + "\n... (truncated)"

        step = Step(
            round=round_no,
            tool=tool,
            reasoning=action.get("reasoning", ""),
            args=args,
            observation=observation,
            elapsed=time.monotonic() - started,
        )
        inv.steps.append(step)
        if on_step:
            on_step(step)
    else:
        inv.stopped_because = f"round limit ({max_rounds}) reached"
        if inv.steps and not inv.finding:
            inv.finding = _summarize_steps(inv)

    return inv


def _summarize_steps(inv: "Investigation") -> str:
    """A factual account of what was searched, for when no conclusion was reached.

    Deliberately not a guess at the cause: the loop stopped early precisely because it
    had not got there, and inventing one here would be the failure mode the whole
    design guards against. Where it looked is still worth knowing.
    """
    lines = ["Investigation did not reach a conclusion. What was checked:"]
    for step in inv.steps:
        target = next(
            (v for k, v in step.args.items() if k in ("pattern", "path", "expr", "repo")),
            "",
        )
        first_line = (step.observation or "").splitlines()[0][:120] if step.observation else ""
        lines.append(f"- {step.tool} {target}: {first_line}")
    return "\n".join(lines)


def _build_prompt(
    context: str, registry: RepoRegistry, inv: Investigation, round_no: int, max_rounds: int
) -> str:
    sections = [
        f"# Alert under investigation\n{context}",
        f"\n# Repositories you can search\n{registry.describe()}",
    ]

    if inv.steps:
        sections.append(f"\n# What you have done so far\n{inv.transcript()}")
    else:
        sections.append("\n# What you have done so far\nNothing yet.")

    sections.append(
        f"\n# This is round {round_no} of at most {max_rounds}\n"
        "Pick the next tool, or conclude. Base the choice on the output above."
    )
    return "\n".join(sections)
