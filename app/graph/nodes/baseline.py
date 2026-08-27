"""The evidence floor: identify, fetch the rule, resolve the workload, measure.

Spec §9 constraint 8 — investigation is unconditional — is implemented here by *position*
as much as by content: this node sits on the only edge into the planner, so every turn
traverses it. A chat turn with no alert in it does not skip the floor; it runs the same
probes, finds nothing to run them on, and produces `SkippedProbe`s instead of observations.
"We did not measure this, and here is why" is a different reply from silence, and silence
is what makes a thin pack read like a complete one.

There is **no model in this node**. Nothing it does is a judgment: `identify()` is a
keyword table, `fetch_rule()` is a four-rung ladder that labels every rung, `resolve()` is a
lookup that states its own confidence. Constraint 7 is enforced one layer down — this node
passes `collect_baseline` only `(identity, rule, resolution, minutes)`, and that function's
signature has nowhere to put `state["priors"]` or `state["input"]` even if a later edit
wanted to.
"""

from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool
from loguru import logger

from app.config import Settings
from app.core import ToolLoader
from app.domain.alerts import benign_checks
from app.domain.deployments import resolve
from app.domain.sources import contract_for
from app.evidence.baseline import collect_baseline
from app.evidence.identify import identify
from app.evidence.rules import fetch_rule
from app.graph.state import OncallState

METRIC_TOOL = "query_metric"
RULE_TOOL = "fetch_alert_rule"

_EXPR_FIELDS = ("expr", "query", "promql", "expression")
_MINUTES_FIELDS = ("minutes", "range_minutes", "window_minutes", "duration_minutes")
_NAME_FIELDS = ("name", "alert_name", "rule_name", "uid", "query")


def _progress(stage: str, message: str, **payload: Any) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:
        return
    if writer:
        writer({"stage": stage, "message": message, **payload})


def _fields(tool: BaseTool) -> set[str]:
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, dict):
        return set((schema.get("properties") or {}).keys())
    fields = getattr(schema, "model_fields", None)
    return set(fields.keys()) if isinstance(fields, dict) else set()


def _pick(candidates: tuple[str, ...], available: set[str], fallback: str) -> str:
    return next((c for c in candidates if c in available), fallback)


def metric_adapter(tool: BaseTool | None) -> Callable[..., Any] | None:
    """The evidence layer's `query_metric(expr, minutes=…)` over whatever the tool calls it.

    An MCP server names its own parameters and the agent does not get to rename them, so the
    argument names are read from the schema. Guessing wrong here fails loudly (a rejected
    call) rather than quietly, which is the right way round.
    """
    if tool is None:
        return None
    available = _fields(tool)
    expr_field = _pick(_EXPR_FIELDS, available, "expr")
    minutes_field = _pick(_MINUTES_FIELDS, available, "minutes")

    async def query_metric(expr: str, minutes: int = 60) -> Any:
        payload: dict[str, Any] = {expr_field: expr}
        if minutes_field in available or not available:
            payload[minutes_field] = minutes
        return await tool.ainvoke(payload)

    return query_metric


def rule_adapter(tool: BaseTool | None) -> Callable[..., Any] | None:
    if tool is None:
        return None
    name_field = _pick(_NAME_FIELDS, _fields(tool), "name")

    async def fetch(alert_name: str) -> Any:
        return await tool.ainvoke({name_field: alert_name})

    return fetch


def baseline_node(settings: Settings, load: ToolLoader) -> Callable[[OncallState], Any]:
    """Node factory. The settings and the toolset are closed over, never read from state."""

    async def baseline(state: OncallState) -> dict[str, Any]:
        text = state.get("alert_text") or state.get("input") or ""
        _progress("baseline", "identifying the alert")

        identity = identify(text)
        tools, warnings = await load()
        for warning in warnings:
            logger.warning(f"tool loading: {warning}")

        by_name = {t.name: t for t in tools}
        metric_tool = by_name.get(METRIC_TOOL)
        query_metric = metric_adapter(metric_tool)

        rule = await fetch_rule(identity, query_metric_tool=rule_adapter(by_name.get(RULE_TOOL)))

        labels = identity.labels
        resolution = resolve(
            host=labels.get("host"),
            path=labels.get("path"),
            app_label=labels.get("app") or labels.get("service") or labels.get("deployment"),
        )

        _progress(
            "baseline",
            f"measuring {identity.alert_name}",
            alert=identity.alert_name,
            deployment=resolution.app_label,
        )

        # Constraint 7: identity, rule, resolution, minutes. Nothing a human said.
        observations, skipped = await collect_baseline(
            identity,
            rule,
            resolution,
            minutes=settings.query_window_minutes,
            query_metric=query_metric,
        )

        update: dict[str, Any] = {
            "identity": identity,
            "rule": rule,
            "resolution": resolution,
            "baseline": observations,
            "skipped": skipped,
            "benign": benign_checks(identity.alert_name),
        }
        if metric_tool is not None and contract_for(metric_tool.name).synthetic:
            update["used_synthetic"] = True

        logger.info(
            f"baseline node: {identity.alert_name} → {resolution.app_label or 'unresolved'} "
            f"({len(observations)} observations, {len(skipped)} skipped)"
        )
        return update

    return baseline
