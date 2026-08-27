"""The alert's own expression, and how far from authoritative it is (tech-design §3.1).

Reproducing the alert with its own query, at its own label granularity, is the rule that
stops a host-scoped failure from being waved away by a site-wide average. An earlier design
made rule lookup the *only* entry into metric collection, which meant an unrecognized alert
produced zero queries — the strictness meant to prevent one wrong number prevented every
number instead.

So the rule degrades down four rungs and never blocks, and every rung below the first is
labelled in `AlertRule.provenance`. The honesty lives in the label, not in refusing to
query: nothing may mistake a reconstruction for the alert's own measurement.
"""

import json
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger

from app.domain.alerts import get_alert
from app.domain.models import AlertIdentity, AlertRule
from app.evidence.identify import links_of

RuleTool = Callable[..., Awaitable[Any] | Any]

PROVENANCE_CAVEAT = {
    "registry": "registry expression — the local fallback, not the alert's own rule",
    "reconstructed": "reconstructed from an alert link — not the alert's own rule",
    "synthesized": "synthesized from alert labels — not the alert's own query",
}


def provenance_caveat(rule: AlertRule | None) -> str:
    """The line that must travel with a rule that is not the authoritative one."""
    if rule is None:
        return ""
    return PROVENANCE_CAVEAT.get(rule.provenance, "")


def _as_rule(name: str, raw: Any) -> AlertRule | None:
    if isinstance(raw, AlertRule):
        return raw
    if isinstance(raw, str):
        expression = raw.strip()
        return AlertRule(name=name, expression=expression) if expression else None
    if isinstance(raw, dict):
        expression = str(raw.get("expression") or raw.get("query") or raw.get("expr") or "").strip()
        if not expression:
            return None
        return AlertRule(
            name=str(raw.get("name") or name),
            expression=expression,
            duration=raw.get("duration"),
            labels={str(k): str(v) for k, v in (raw.get("labels") or {}).items()},
        )
    return None


def _expr_in(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("expr", "query") and isinstance(item, str) and item.strip():
                return item.strip()
            found = _expr_in(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _expr_in(item)
            if found:
                return found
    return None


def expression_from_link(url: str) -> str | None:
    """Best effort: the query a monitoring link represents, read from its own parameters.

    Deliberately not a parser per system (tech-design §3.6): six URL dialects rot
    independently, and a stale parser's malformed query comes back empty, which is
    indistinguishable from healthy. This reads the parameters that carry a query verbatim
    and gives up otherwise. The URL is never fetched here.
    """
    try:
        params = parse_qs(urlparse(url).query)
    except ValueError:
        return None

    for key, values in params.items():
        if key.rsplit(".", 1)[-1] in ("expr", "query") and values and values[0].strip():
            return values[0].strip()

    for values in params.values():
        for value in values:
            if "expr" not in value:
                continue
            try:
                found = _expr_in(json.loads(value))
            except (TypeError, ValueError):
                continue
            if found:
                return found
    return None


def synthesized_expression(labels: dict[str, str]) -> str | None:
    """An error-rate expression for whatever the alert text named (tech-design §3.5)."""
    host = labels.get("host")
    path = labels.get("path")
    app = labels.get("app") or labels.get("service") or labels.get("deployment")

    if host and path:
        return (
            f'sum(rate(nginx_ingress_controller_requests{{host="{host}",'
            f'path="{path}",status=~"5.."}}[5m])) by (host, path)'
        )
    if host:
        return (
            f'sum(rate(nginx_ingress_controller_requests{{host="{host}",'
            'status=~"5.."}[5m])) by (host)'
        )
    if path:
        return (
            f'sum(rate(nginx_ingress_controller_requests{{path="{path}",'
            'status=~"5.."}[5m])) by (host)'
        )
    if app:
        return f'sum(rate(server_api_requests_total{{app="{app}",status=~"5.."}}[5m])) by (app)'
    return None


async def fetch_rule(
    identity: AlertIdentity, *, query_metric_tool: RuleTool | None = None
) -> AlertRule | None:
    """Walk the ladder until something answers. Returning `None` is allowed, blocking is not.

    `query_metric_tool` is the Grafana rule-lookup callable (rung 1). It is optional and its
    failure is a rung, not an exception: a metrics backend that is down must not stop the run
    that would have said so.
    """
    name = identity.alert_name

    if query_metric_tool is not None:
        try:
            raw = query_metric_tool(name)
            if isawaitable(raw):
                raw = await raw
            rule = _as_rule(name, raw)
            if rule:
                return rule.model_copy(update={"provenance": "authoritative"})
        except Exception as exc:
            logger.warning(f"authoritative rule lookup failed for {name!r}: {exc}")

    known = get_alert(name)
    if known and known.fallback_expr:
        return AlertRule(name=name, expression=known.fallback_expr, provenance="registry")

    for url in links_of(identity):
        expression = expression_from_link(url)
        if expression:
            return AlertRule(
                name=name,
                expression=expression,
                labels={"source_link": url},
                provenance="reconstructed",
            )

    expression = synthesized_expression(identity.labels)
    if expression:
        return AlertRule(name=name, expression=expression, provenance="synthesized")

    logger.info(f"no rule and nothing to synthesize from for alert {name!r}")
    return None
