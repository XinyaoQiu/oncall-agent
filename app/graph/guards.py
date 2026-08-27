"""The limits, in code, where the model has no vote (spec §8).

The sibling implementation put every one of these in prompt text — "新步骤数量必须 <= 当前
剩余步骤数", "已执行 >= 5 次时禁止 replan" — and then re-checked two of the four in Python
afterwards. A limit expressed as an instruction is a limit the model can decline, and the
decline is invisible: an eleven-step plan looks exactly like a three-step plan that took
longer.

So each rule here is a function that the graph calls, and the prompt is free to say nothing
about it:

- `should_stop` ends the loop on step count or wall clock.
- `clamp_replan` truncates. "Add three more probes" is not refused, it is unrepresentable.
- `seen_call_signature` short-circuits a repeat: an identical call cannot produce a new
  observation, so spending a round rediscovering that is spending it on nothing.
- `missing_required_args` rejects *with the field names*. A bare "tool error" leaves the
  model to guess, and it guesses by reissuing the same broken call until the budget is gone.

One detail that is easy to get wrong: the budget's start is a **wall-clock** stamp, not
`time.monotonic()`. Monotonic is process-relative — written into a checkpoint by `slackd`
and read back by `oncall-api` it is not merely inaccurate, it is meaningless, and the
resulting deadline is silently wrong in either direction.
"""

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.graph.state import OncallState

SIGNATURE_SEP = " | "

# Arguments without which a tool cannot run at all. Tools that carry an args_schema are
# read from it; this table covers the ones loaded over MCP with a loose schema.
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "search_code": ("pattern",),
    "read_file": ("repo", "path"),
    "git_log": ("repo",),
    "list_dir": ("repo",),
    "query_metric": ("expr",),
    "retrieve_knowledge": ("query",),
}


@dataclass(frozen=True)
class Budget:
    """How much of this turn is left to spend.

    `started_at` is `time.time()` — a wall-clock stamp, so it still means something after a
    checkpoint round-trip through another process. The deadline is derived as
    `started_at + wall_clock_seconds` rather than stored, so a resumed turn is measured
    against the same absolute instant the original one was.
    """

    started_at: float
    max_steps: int = 8
    wall_clock_seconds: float = 120.0
    replan_ban_after: int = 5

    @classmethod
    def from_settings(cls, settings: Settings, started_at: float | None = None) -> "Budget":
        return cls(
            started_at=started_at if started_at is not None else time.time(),
            max_steps=settings.max_steps,
            wall_clock_seconds=settings.wall_clock_seconds,
            replan_ban_after=settings.replan_ban_after,
        )

    @property
    def deadline(self) -> float:
        return self.started_at + self.wall_clock_seconds

    def elapsed(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.started_at

    def remaining_seconds(self, now: float | None = None) -> float:
        return max(0.0, self.deadline - (now if now is not None else time.time()))


def budget_from_config(settings: Settings, config: Mapping[str, Any] | None) -> Budget:
    """The turn's budget, anchored on the stamp `run_turn` put in the run config.

    Falling back to "now" is deliberate: a caller that forgot the stamp gets a full budget
    from this moment, which is generous but bounded. Inheriting a stamp from graph *build*
    time would silently give every turn after the first a budget of zero.
    """
    configurable = (config or {}).get("configurable") or {}
    started = configurable.get("started_at")
    return Budget.from_settings(settings, started if isinstance(started, (int, float)) else None)


def should_stop(state: OncallState, budget: Budget, *, now: float | None = None) -> str | None:
    """Why this turn must stop, or `None` to keep going.

    Both limits are checked because they fail differently: step count bounds the cost of a
    model that will not converge, wall clock bounds the cost of tools that will not return.
    A thorough answer delivered after the outage is over is worth nothing.
    """
    steps = len(state.get("past_steps") or [])
    if steps >= budget.max_steps:
        return f"step budget ({budget.max_steps} steps) reached"

    elapsed = budget.elapsed(now)
    if elapsed >= budget.wall_clock_seconds:
        return f"wall-clock budget ({budget.wall_clock_seconds:.0f}s) reached after {elapsed:.0f}s"
    return None


def replan_banned(state: OncallState, budget: Budget) -> str | None:
    """Past this many executed steps, re-planning is off the table.

    A plan rewritten late is a plan whose earlier steps were wasted; at this point the
    honest move is to answer from what was measured.
    """
    steps = len(state.get("past_steps") or [])
    if steps >= budget.replan_ban_after:
        return (
            f"re-planning was declined after {steps} steps; answering from what was "
            "already measured"
        )
    return None


def clamp_replan(new_steps: Iterable[str], remaining: Iterable[str]) -> list[str]:
    """A replan may reshape the remaining work. It may never grow it.

    Truncation, not rejection: the model's re-ordering is usually worth keeping, and the
    part that would extend the loop is simply not carried. `new_steps[:len(remaining)]`.
    """
    cleaned = [s.strip() for s in new_steps if isinstance(s, str) and s.strip()]
    return cleaned[: len(list(remaining))]


def _compact(args: Any) -> str:
    if isinstance(args, Mapping):
        try:
            return json.dumps(
                {k: v for k, v in sorted(args.items()) if v not in (None, "")},
                ensure_ascii=False,
                default=str,
            )
        except TypeError:
            return str(dict(args))
    return str(args)


def call_signature(tool: str, args: Any) -> str:
    return f"{tool}({_compact(args)})"


def recorded_signatures(state: OncallState) -> set[str]:
    """Every tool call this turn has already made.

    They ride in `ExecutedStep.tool`, joined by `SIGNATURE_SEP`, because the state contract
    has no separate channel for them and inventing one would fork the schema.
    """
    out: set[str] = set()
    for step in state.get("past_steps") or []:
        for part in (step.tool or "").split(SIGNATURE_SEP):
            if part.strip():
                out.add(part.strip())
    return out


def seen_call_signature(state: OncallState, tool: str, args: Any) -> bool:
    """Whether this exact call already ran. If it did, running it again buys nothing."""
    return call_signature(tool, args) in recorded_signatures(state)


def duplicate_message(tool: str, args: Any) -> str:
    return (
        f"Call rejected: {call_signature(tool, args)} already ran this turn and its result "
        "is in the history above. Repeating it cannot return anything new — use a different "
        "tool, repo, or search term."
    )


def _schema_required(tool: Any) -> tuple[str, ...]:
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return ()
    if isinstance(schema, Mapping):
        required = schema.get("required")
        return tuple(str(f) for f in required) if isinstance(required, list) else ()
    fields = getattr(schema, "model_fields", None)
    if not isinstance(fields, Mapping):
        return ()
    return tuple(name for name, field in fields.items() if field.is_required())


def missing_required_args(tool: Any, args: Any) -> list[str]:
    """Which required fields this call left out, by name.

    The names are the whole point. `investigate()`'s comment in the old repo says it
    plainly: a bare error leaves the model to guess, and it guesses by reissuing the same
    broken call.
    """
    name = getattr(tool, "name", tool)
    required = set(REQUIRED_ARGS.get(str(name), ())) | set(_schema_required(tool))
    supplied = args if isinstance(args, Mapping) else {}
    return [
        field for field in sorted(required) if supplied.get(field) in (None, "", [], {})
    ]


def rejection_message(tool: str, missing: list[str], args: Any) -> str:
    was = "was" if len(missing) == 1 else "were"
    return (
        f"Call rejected: {tool} requires {', '.join(missing)}, which {was} not provided. "
        f"Received: {_compact(args) or '(nothing)'}. Supply the missing field and call again."
    )
