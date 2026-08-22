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

    def test_populated_result_shows_range_and_latest(self):
        result = MetricResult(
            query="up",
            series=[MetricSeries(labels={"app": "x"}, points=[(0, 1.0), (1, 5.0)])],
        )
        summary = result.summarize()
        assert "min=1" in summary and "max=5" in summary and "latest=5" in summary

    def test_flat_series_reads_as_steady(self):
        result = MetricResult(
            query="up",
            series=[MetricSeries(labels={"app": "x"}, points=[(0, 3.0), (1, 3.0)])],
        )
        assert "steady at 3" in result.summarize()


class TestTimestampMetrics:
    """A pod start time is a moment, not a magnitude.

    Rendering it as a raw epoch number let the model read any timestamp as "recent"
    and cite it as evidence of a fresh deploy.
    """

    def test_start_time_renders_as_age(self):
        now = 1_787_000_000.0
        result = MetricResult(
            query='kube_pod_start_time{pod=~"server-feed-.*"}',
            series=[MetricSeries(labels={"pod": "server-feed-abc"}, points=[(now, now - 720)])],
        )
        summary = result.summarize(now=now)
        assert "12 minutes ago" in summary
        assert "1.787e+09" not in summary

    def test_old_start_time_is_not_described_as_recent(self):
        now = 1_787_000_000.0
        result = MetricResult(
            query='kube_pod_start_time{pod=~"server-feed-.*"}',
            series=[
                MetricSeries(labels={"pod": "server-feed-abc"}, points=[(now, now - 86400 * 3)])
            ],
        )
        assert "3.0 days ago" in result.summarize(now=now)

    def test_value_metrics_are_not_treated_as_timestamps(self):
        result = MetricResult(
            query="kube_deployment_status_replicas{deployment='x'}",
            series=[MetricSeries(labels={"deployment": "x"}, points=[(0, 4.0), (1, 12.0)])],
        )
        assert "ago" not in result.summarize()


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


class TestSampleDataDisclosure:
    """Fixtures must never be presentable as live measurements.

    A reader cannot tell sample numbers from real ones by looking at them, so the
    warning goes both into the prompt and into the visible reply.
    """

    def _sample_result(self):
        from oncall_agent.models import TriageResult

        grafana = GrafanaClient(Settings(use_sample_metrics=True))
        identity = AlertIdentity(alert_name="news-list-for-channel-p99")
        rule = evidence.fetch_rule(grafana, identity)
        deployment = evidence.resolve_service(identity, rule)
        metrics = evidence.gather(grafana, identity, rule, deployment)
        return TriageResult(identity=identity, rule=rule, metrics=metrics), deployment

    def test_sample_caveat_stays_out_of_the_evidence(self):
        from oncall_agent.analysis.diagnose import build_prompt

        result, deployment = self._sample_result()
        prompt = build_prompt(result.identity, result.rule, deployment, result.metrics, [], [], [])

        # Anything sitting among the evidence gets cited as evidence — the engineer then
        # reads about the prompt's own sections instead of about their outage.
        assert "fixture" not in prompt.lower()
        assert "sample" not in prompt.lower()
        assert "system prompt" not in prompt.lower()

    def test_prompt_includes_current_time(self):
        from oncall_agent.analysis.diagnose import build_prompt

        result, deployment = self._sample_result()
        prompt = build_prompt(result.identity, result.rule, deployment, result.metrics, [], [], [])

        # Without a reference point the model cannot tell whether a timestamp is recent.
        assert "# Now" in prompt

    def test_reply_carries_the_warning(self):
        from oncall_agent.pipeline import format_reply

        result, _ = self._sample_result()
        assert "Sample metrics" in format_reply(result)

    def test_real_metrics_carry_no_warning(self):
        from oncall_agent.models import TriageResult
        from oncall_agent.pipeline import format_reply

        result = TriageResult(
            identity=AlertIdentity(alert_name="large-scale-5xx"),
            metrics=[MetricResult(query="up", source="grafana", series=[])],
        )
        assert "Sample metrics" not in format_reply(result)


class TestImpactQuantification:
    """Step 7 of the playbook: how much is actually affected.

    Counts come from the ingress layer because application logs are sampled ~1/8 —
    counting there understates impact by nearly an order of magnitude, and the wrong
    number looks authoritative once it reaches an incident review.
    """

    def test_impact_queries_target_the_ingress_layer(self, sample_grafana):
        identity = identify(FIVEXX_ALERT, llm=None)
        results = evidence.quantify_impact(sample_grafana, identity, None)

        assert results, "a host-scoped alert should produce impact queries"
        assert all("nginx_ingress_controller_requests" in r.query for r in results)
        assert not any("server_logs" in r.query for r in results)

    def test_impact_covers_errors_and_total(self, sample_grafana):
        identity = identify(FIVEXX_ALERT, llm=None)
        queries = [r.query for r in evidence.quantify_impact(sample_grafana, identity, None)]

        # Both are needed: an error count alone cannot express a rate.
        assert any('status=~"5.."' in q for q in queries)
        assert any('status' not in q for q in queries)

    def test_impact_is_scoped_to_the_alerting_host(self, sample_grafana):
        identity = identify(FIVEXX_ALERT, llm=None)
        results = evidence.quantify_impact(sample_grafana, identity, None)

        hosts = {s.labels.get("host") for r in results for s in r.series}
        assert hosts == {"www.newsbreak.com"}

    def test_no_host_means_no_impact_query(self, sample_grafana):
        identity = AlertIdentity(alert_name="feed-channel-empty", labels={})
        assert evidence.quantify_impact(sample_grafana, identity, None) == []

    def test_gather_includes_impact(self, sample_grafana):
        identity = identify(FIVEXX_ALERT, llm=None)
        rule = evidence.fetch_rule(sample_grafana, identity)
        deployment = evidence.resolve_service(identity, rule)
        results = evidence.gather(sample_grafana, identity, rule, deployment)

        assert any("nginx_ingress_controller_requests{host" in r.query for r in results)


class TestQuestionRouting:
    def test_question_reaches_the_prompt(self, sample_grafana):
        from oncall_agent.analysis.diagnose import build_prompt

        identity = identify(FIVEXX_ALERT, llm=None)
        prompt = build_prompt(
            identity, None, None, [], [], [], [], question="find production influence"
        )
        assert "find production influence" in prompt
        assert "Answer this first" in prompt

    def test_prompt_omits_the_section_without_a_question(self):
        from oncall_agent.analysis.diagnose import build_prompt

        identity = identify(FIVEXX_ALERT, llm=None)
        assert "engineer asked" not in build_prompt(identity, None, None, [], [], [], [])


class TestTierFallback:
    """When the deep tier is overloaded, a weaker answer beats no answer — but the
    reader must be told, since the fallback changes analysis quality."""

    def test_reply_flags_a_degraded_tier(self):
        from oncall_agent.models import Confidence, Diagnosis, TriageResult
        from oncall_agent.pipeline import format_reply

        result = TriageResult(
            identity=AlertIdentity(alert_name="large-scale-5xx"),
            diagnosis=Diagnosis(
                summary="s", likely_cause="c", confidence=Confidence.LOW,
                victim_or_cause="v", model="gemini-flash-latest", degraded_tier=True,
            ),
        )
        reply = format_reply(result)
        assert "gemini-flash-latest" in reply
        assert "overloaded" in reply

    def test_normal_tier_is_not_flagged(self):
        from oncall_agent.models import Confidence, Diagnosis, TriageResult
        from oncall_agent.pipeline import format_reply

        result = TriageResult(
            identity=AlertIdentity(alert_name="large-scale-5xx"),
            diagnosis=Diagnosis(
                summary="s", likely_cause="c", confidence=Confidence.LOW,
                victim_or_cause="v", model="gemini-pro-latest", degraded_tier=False,
            ),
        )
        assert "overloaded" not in format_reply(result)

    def test_transient_errors_are_recognised(self):
        from oncall_agent.llm import _is_transient

        assert _is_transient(Exception("503 UNAVAILABLE"))
        assert _is_transient(Exception("429 RESOURCE_EXHAUSTED"))
        assert not _is_transient(Exception("404 NOT_FOUND"))
        assert not _is_transient(Exception("401 UNAUTHENTICATED"))


class TestKnowledgeTerms:
    def test_terms_include_the_resolved_deployment(self, sample_grafana):
        from oncall_agent.pipeline import _knowledge_terms

        identity = identify(CHANNEL_ALERT, llm=None)
        rule = evidence.fetch_rule(sample_grafana, identity)
        deployment = evidence.resolve_service(identity, rule)

        terms = _knowledge_terms(identity.alert_name, identity.labels, deployment)
        # Resolved from the rule expression, so a label-only search would miss the
        # history of the very service that is alerting.
        assert "server-feed" in terms
        assert "api.newsbreak.com" in terms

    def test_terms_are_deduplicated(self, sample_grafana):
        from oncall_agent.pipeline import _knowledge_terms

        identity = identify(CHANNEL_ALERT, llm=None)
        rule = evidence.fetch_rule(sample_grafana, identity)
        deployment = evidence.resolve_service(identity, rule)

        terms = _knowledge_terms(identity.alert_name, identity.labels, deployment)
        assert len(terms) == len(set(terms))


class TestMetricAccounting:
    """Queries issued, queries with data, and series found are three numbers.

    Reporting only the first reads as "we looked and it's fine" when nothing came back.
    """

    def test_empty_queries_are_reported_as_empty(self):
        from oncall_agent.models import TriageResult
        from oncall_agent.pipeline import format_reply

        result = TriageResult(
            identity=AlertIdentity(alert_name="large-scale-5xx"),
            metrics=[
                MetricResult(query="a", series=[MetricSeries(labels={}, points=[(0, 1.0)])]),
                MetricResult(query="b", series=[]),
            ],
        )
        reply = format_reply(result)
        assert "2 queries" in reply
        assert "1 empty" in reply

    def test_failed_queries_are_reported_separately(self):
        from oncall_agent.models import TriageResult
        from oncall_agent.pipeline import format_reply

        result = TriageResult(
            identity=AlertIdentity(alert_name="large-scale-5xx"),
            metrics=[MetricResult(query="a", error="timeout")],
        )
        assert "1 failed" in format_reply(result)
