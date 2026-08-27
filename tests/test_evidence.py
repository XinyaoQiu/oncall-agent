"""The deterministic floor. Spec §9 constraints 1, 3, 4, 7 and 8.

Two of these tests assert a function signature. That is not pedantry: the signature is the
only mechanism that survives a rewrite of every prompt in the system.
"""

import inspect
import time

import pytest

from app.evidence.accounting import metric_accounting
from app.evidence.baseline import collect_baseline
from app.evidence.envelope import Observation, Series
from app.evidence.identify import extract_labels, identify
from app.evidence.impact import quantify_impact
from app.evidence.rules import expression_from_link, fetch_rule
from app.graph.state import AlertIdentity, AlertRule, Resolution

CHANNEL_ALERT = """
[FIRING:1] news-list-for-channel p99 latency
host = api.newsbreak.com
path = /Website/channel/news-list-for-channel
Panel: https://grafana.internal/d/abc123?panelId=3&from=now-1h
"""


class Recorder:
    """A metric backend that records every expression it was asked for."""

    def __init__(self, series=None, fail=False):
        self.queries: list[str] = []
        self._series = series or []
        self._fail = fail

    async def __call__(self, expr: str, *, minutes: int = 60) -> Observation:
        self.queries.append(expr)
        if self._fail:
            raise RuntimeError("grafana timeout")
        return Observation(query=expr, series=list(self._series))


def _series(value: float = 1.0) -> list[Series]:
    return [Series(labels={"host": "api.newsbreak.com"}, points=[(time.time(), value)])]


class TestSignaturesAreTheMechanism:
    """§9 constraints 7 and 1. A prompt can be rewritten; a signature fails CI."""

    def test_baseline_signature_is_frozen(self):
        params = inspect.signature(collect_baseline).parameters
        assert set(params) == {"identity", "rule", "resolution", "minutes", "query_metric"}

    def test_baseline_takes_no_thread_context(self):
        params = set(inspect.signature(collect_baseline).parameters)
        for forbidden in ("question", "input", "priors", "thread", "extra_probes", "state"):
            assert forbidden not in params

    def test_impact_has_no_source_parameter(self):
        params = inspect.signature(quantify_impact).parameters
        assert set(params) == {"host", "minutes", "query_metric"}
        assert "source" not in params

    def test_minutes_and_query_metric_are_keyword_only(self):
        params = inspect.signature(collect_baseline).parameters
        assert params["minutes"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["query_metric"].kind is inspect.Parameter.KEYWORD_ONLY


class TestIdentify:
    def test_known_alert_is_matched_by_keyword(self):
        assert identify(CHANNEL_ALERT).alert_name == "news-list-for-channel-p99"

    def test_interesting_labels_are_extracted(self):
        labels = extract_labels(CHANNEL_ALERT)
        assert labels["host"] == "api.newsbreak.com"
        assert labels["path"] == "/Website/channel/news-list-for-channel"

    def test_prose_is_not_treated_as_a_label(self):
        assert "severity" not in extract_labels("severity = page, owner = feed")

    def test_links_are_extracted(self):
        links = extract_labels(CHANNEL_ALERT)["_links"]
        assert links.startswith("https://grafana.internal/d/abc123")

    def test_unknown_alert_still_carries_its_labels(self):
        identity = identify("something nobody has ever seen  app=billing-svc")
        assert identity.alert_name == "unknown"
        assert identity.labels["app"] == "billing-svc"


class TestRuleLadder:
    """Every rung below the first is labelled, so nothing reads a reconstruction as the
    alert's own measurement."""

    async def test_grafana_rule_is_authoritative(self):
        async def tool(name: str):
            return {"name": name, "expression": "up{job='x'}"}

        rule = await fetch_rule(identify(CHANNEL_ALERT), query_metric_tool=tool)
        assert rule.provenance == "authoritative"
        assert rule.expression == "up{job='x'}"

    async def test_registry_fallback_is_labelled(self):
        rule = await fetch_rule(identify(CHANNEL_ALERT))
        assert rule.provenance == "registry"
        assert "server_api_delay_bucket" in rule.expression

    async def test_tool_failure_falls_to_the_next_rung(self):
        async def tool(name: str):
            raise RuntimeError("grafana down")

        rule = await fetch_rule(identify(CHANNEL_ALERT), query_metric_tool=tool)
        assert rule.provenance == "registry"

    async def test_reconstructed_from_a_link(self):
        identity = AlertIdentity(
            alert_name="unknown",
            labels={"_links": "https://prom.internal/graph?g0.expr=sum(rate(foo[5m]))"},
        )
        rule = await fetch_rule(identity)
        assert rule.provenance == "reconstructed"
        assert rule.expression == "sum(rate(foo[5m]))"

    def test_explore_json_link_is_read(self):
        url = 'https://grafana.internal/explore?left={"queries":[{"expr":"sum(bar)"}]}'
        assert expression_from_link(url) == "sum(bar)"

    async def test_synthesized_from_labels_alone(self):
        identity = AlertIdentity(alert_name="unknown", labels={"host": "www.newsbreak.com"})
        rule = await fetch_rule(identity)
        assert rule.provenance == "synthesized"
        assert 'host="www.newsbreak.com"' in rule.expression

    async def test_nothing_to_go_on_is_none_not_an_exception(self):
        assert await fetch_rule(AlertIdentity()) is None


class TestBaselineOrdering:
    async def test_alert_query_is_observation_zero(self):
        backend = Recorder(series=_series())
        rule = AlertRule(name="a", expression="ALERT_EXPR")
        observations, _ = await collect_baseline(
            identify(CHANNEL_ALERT),
            rule,
            Resolution(app_label="server-a4api-web", confidence="exact", matched_by="path"),
            query_metric=backend,
        )
        assert observations[0].query == "ALERT_EXPR"
        assert backend.queries[0] == "ALERT_EXPR"

    async def test_missing_rule_is_reported_not_silent(self):
        observations, skipped = await collect_baseline(
            identify(CHANNEL_ALERT), None, Resolution(), query_metric=Recorder()
        )
        assert any(p.probe == "the alert's own query" for p in skipped)
        assert observations, "a missing rule must not stop collection"

    async def test_unknown_alert_still_produces_observations(self):
        """§9 constraint 8: recognition buys a shortcut, never permission to investigate."""
        identity = AlertIdentity(alert_name="unknown", labels={"app": "billing-svc"})
        rule = await fetch_rule(identity)
        observations, _ = await collect_baseline(
            identity, rule, Resolution(), query_metric=Recorder(series=_series())
        )
        assert len(observations) >= 3
        assert any("billing-svc" in o.query for o in observations)

    async def test_named_app_absent_from_the_table_says_so(self):
        identity = AlertIdentity(alert_name="unknown", labels={"app": "billing-svc"})
        observations, _ = await collect_baseline(
            identity, None, Resolution(), query_metric=Recorder(series=_series())
        )
        caveats = [c for o in observations for c in o.all_caveats()]
        assert any("not in the deployment table" in c for c in caveats)

    async def test_low_confidence_resolution_attaches_a_caveat(self):
        resolution = Resolution(
            app_label="server-default",
            confidence="low",
            matched_by="catch-all",
            note="path fell through to the catch-all",
        )
        observations, _ = await collect_baseline(
            AlertIdentity(alert_name="unknown"),
            None,
            resolution,
            query_metric=Recorder(series=_series()),
        )
        rendered = "\n".join(o.render() for o in observations)
        assert "unconfirmed workload" in rendered
        assert "catch-all" in rendered

    async def test_exact_resolution_carries_no_workload_caveat(self):
        observations, _ = await collect_baseline(
            AlertIdentity(alert_name="unknown"),
            None,
            Resolution(app_label="server-feed", confidence="exact", matched_by="app_label"),
            query_metric=Recorder(series=_series()),
        )
        assert not any("unconfirmed workload" in c for o in observations for c in o.all_caveats())

    async def test_no_backend_skips_everything_with_reasons(self):
        observations, skipped = await collect_baseline(
            identify(CHANNEL_ALERT), AlertRule(name="a", expression="X"), Resolution()
        )
        assert observations == []
        assert len(skipped) == 3

    async def test_query_failure_is_an_error_not_emptiness(self):
        observations, _ = await collect_baseline(
            identify(CHANNEL_ALERT),
            AlertRule(name="a", expression="X"),
            Resolution(),
            query_metric=Recorder(fail=True),
        )
        assert observations[0].error
        assert "query failed" in observations[0].render()

    async def test_provenance_of_a_synthesized_rule_rides_with_it(self):
        rule = AlertRule(name="a", expression="X", provenance="synthesized")
        observations, _ = await collect_baseline(
            AlertIdentity(), rule, Resolution(), query_metric=Recorder(series=_series())
        )
        assert "not the alert's own query" in observations[0].render()


class TestImpact:
    async def test_impact_queries_the_ingress(self):
        backend = Recorder(series=_series())
        observations, _ = await quantify_impact("www.newsbreak.com", query_metric=backend)
        assert len(observations) == 2
        assert all("nginx_ingress_controller_requests" in q for q in backend.queries)

    async def test_impact_covers_errors_and_total(self):
        backend = Recorder(series=_series())
        await quantify_impact("www.newsbreak.com", query_metric=backend)
        assert any('status=~"5.."' in q for q in backend.queries)
        assert any('status=~"5.."' not in q for q in backend.queries)

    async def test_impact_is_scoped_to_the_alerting_host(self):
        backend = Recorder(series=_series())
        await quantify_impact("www.newsbreak.com", query_metric=backend)
        assert all('host=~"www.newsbreak.com"' in q for q in backend.queries)

    async def test_no_host_is_a_reported_skip(self):
        observations, skipped = await quantify_impact(None, query_metric=Recorder())
        assert observations == []
        assert "nobody asked" in skipped[0].reason

    async def test_impact_carries_its_source_contract(self):
        observations, _ = await quantify_impact("www.newsbreak.com", query_metric=Recorder())
        assert observations[0].contract.is_impact_source

    async def test_baseline_delegates_impact_to_the_host(self):
        backend = Recorder(series=_series())
        await collect_baseline(identify(CHANNEL_ALERT), None, Resolution(), query_metric=backend)
        assert any("nginx_ingress_controller_requests" in q for q in backend.queries)


class TestEmptyIsNotHealthy:
    """§9 constraint 4."""

    async def test_empty_series_renders_not_healthy(self):
        observations, _ = await collect_baseline(
            identify(CHANNEL_ALERT),
            AlertRule(name="a", expression="X"),
            Resolution(),
            query_metric=Recorder(series=[]),
        )
        assert "not the same as 'healthy'" in observations[0].render()

    def test_accounting_separates_empty_from_failed(self):
        counts = metric_accounting(
            [
                Observation(query="a", series=_series()),
                Observation(query="b"),
                Observation(query="c", error="timeout"),
            ]
        )
        assert counts == {"queries": 3, "with_data": 1, "empty": 1, "failed": 1, "series": 1}

    def test_accounting_of_nothing_is_zeroes(self):
        assert metric_accounting([])["queries"] == 0


@pytest.mark.parametrize("module", ["identify", "rules", "baseline", "impact", "accounting"])
def test_no_llm_in_the_evidence_layer(module):
    """The floor is measurement. A model in it is a model deciding what gets measured."""
    import importlib

    source = inspect.getsource(importlib.import_module(f"app.evidence.{module}"))
    for banned in ("langchain_", "import openai", "generate_json", "ChatOpenAI"):
        assert banned not in source
