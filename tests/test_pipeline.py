"""Tests for the parts that need no model."""

import pytest

from oncall_agent.alerts import get_alert, match_alert
from oncall_agent.analysis import evidence
from oncall_agent.analysis.identify import extract_labels, identify
from oncall_agent.config import Settings, resolve_blast_radius, resolve_deployment
from oncall_agent.models import AlertIdentity, MetricResult, MetricSeries
from oncall_agent.sources.grafana import GrafanaClient

CHANNEL_ALERT = """[FIRING] news-list-for-channel p99 latency
app=server-feed path=/Website/channel/news-list-for-channel
value: 3.2s (threshold 2s)"""

FIVEXX_ALERT = """[FIRING] High 5xx error rate
host=www.newsbreak.com
value: 18% (threshold 5%)"""


@pytest.fixture
def sample_grafana():
    return GrafanaClient(Settings(use_sample_metrics=True))


class TestIdentification:
    def test_matches_known_alert_by_keyword(self):
        assert match_alert(CHANNEL_ALERT).name == "news-list-for-channel-p99"

    def test_extracts_labels(self):
        labels = extract_labels(CHANNEL_ALERT)
        assert labels["app"] == "server-feed"
        assert labels["path"] == "/Website/channel/news-list-for-channel"

    def test_unknown_alert_without_model(self):
        identity = identify("something entirely unfamiliar", llm=None)
        assert identity.alert_name == "unknown"
        assert identity.confidence.value == "low"


class TestDeploymentResolution:
    def test_longest_path_prefix_wins(self):
        # Both server-default ("/") and server-feed match; the more specific one should win.
        d = resolve_deployment(host="api.newsbreak.com", path="/Website/channel/news-list")
        assert d.app_label == "server-feed"

    def test_web_path_resolves_to_web_deployment(self):
        d = resolve_deployment(host="www.newsbreak.com", path="/Website/web/home")
        assert d.app_label == "server-a4api-web"

    def test_blast_radius_is_the_inverse(self):
        deployments = resolve_blast_radius("server/router/feed.go")
        assert [d.app_label for d in deployments] == ["server-feed"]


class TestEvidence:
    def test_rule_lookup_recovers_expression(self, sample_grafana):
        identity = AlertIdentity(alert_name="news-list-for-channel-p99")
        rule = evidence.fetch_rule(sample_grafana, identity)
        assert "server_api_delay_bucket" in rule.expression

    def test_service_resolved_from_rule_when_labels_are_missing(self, sample_grafana):
        # The rendered Slack text often drops labels; the rule still carries the selector.
        identity = AlertIdentity(alert_name="news-list-for-channel-p99", labels={})
        rule = evidence.fetch_rule(sample_grafana, identity)
        assert evidence.resolve_service(identity, rule).app_label == "server-feed"

    def test_gather_queries_the_alert_expression_first(self, sample_grafana):
        identity = AlertIdentity(alert_name="news-list-for-channel-p99")
        rule = evidence.fetch_rule(sample_grafana, identity)
        deployment = evidence.resolve_service(identity, rule)
        results = evidence.gather(sample_grafana, identity, rule, deployment)

        assert results[0].query == rule.expression
        assert len(results) > 1

    def test_benign_checks_present_for_known_alert(self):
        checks = evidence.benign_checks(AlertIdentity(alert_name="news-list-for-channel-p99"))
        assert any("cold start" in c for c in checks)

    def test_five_xx_alert_resolves_to_web(self, sample_grafana):
        identity = identify(FIVEXX_ALERT, llm=None)
        assert identity.alert_name == "large-scale-5xx"
        assert identity.labels["host"] == "www.newsbreak.com"


class TestEmptyResults:
    def test_empty_result_is_not_reported_as_healthy(self):
        result = MetricResult(query="up", series=[])
        assert result.is_empty
        assert "not the same as 'healthy'" in result.summarize()

    def test_failed_query_reports_the_failure(self):
        result = MetricResult(query="up", error="connection refused")
        assert "failed" in result.summarize()

    def test_populated_result_summarizes_peak(self):
        result = MetricResult(
            query="up",
            series=[MetricSeries(labels={"app": "x"}, points=[(0, 1.0), (1, 5.0)])],
        )
        assert "peak=5" in result.summarize()


class TestSampleData:
    def test_replica_series_oscillates(self, sample_grafana):
        """The 2026-06-10 shape: HPA thrash, not a smooth ramp."""
        result = sample_grafana.replica_count("server-a4api-web")
        values = [v for _, v in result.series[0].points]
        assert len(set(values)) > 1
        assert max(values) > min(values)

    def test_alert_registry_and_sample_rules_agree(self):
        from oncall_agent.sources import sample_metrics

        for name in ("news-list-for-channel-p99", "large-scale-5xx"):
            assert get_alert(name) is not None
            assert sample_metrics.rule_for(name) is not None
