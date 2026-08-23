"""Tests for the parts that need no model."""

import pytest

from oncall_agent.alerts import get_alert, match_alert
from oncall_agent.analysis import evidence
from oncall_agent.analysis.identify import extract_labels, identify
from oncall_agent.analysis import diagnose
from oncall_agent.config import (
    Settings,
    resolve,
    resolve_blast_radius,
    resolve_deployment,
)
from oncall_agent.models import AlertIdentity, MetricResult, MetricSeries, TriageResult
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


class TestResolutionConfidence:
    """A guess must never be returned looking like a match.

    The failure this guards against is silent: the catch-all returns a real deployment,
    its pod and replica queries succeed, and the pack describes the wrong workload in
    exactly the format it would use for the right one.
    """

    def test_unknown_host_resolves_to_nothing(self):
        r = resolve(host="foo.example.com")
        assert r.deployment is None
        assert r.confidence == "unresolved"

    def test_unknown_host_with_path_resolves_to_nothing(self):
        r = resolve(host="foo.example.com", path="/x")
        assert r.deployment is None

    def test_unknown_path_is_flagged_not_trusted(self):
        r = resolve(host="api.newsbreak.com", path="/api/v2/charge")
        # The catch-all still answers — dropping it would empty the pack — but it says so.
        assert r.deployment.app_label == "server-default"
        assert r.confidence == "low"
        assert r.matched_by == "catch-all"
        assert not r.is_confident

    def test_host_without_path_is_a_guess(self):
        r = resolve(host="api.newsbreak.com")
        assert r.confidence == "low"
        assert r.matched_by == "host-only"

    def test_real_prefix_match_is_exact(self):
        r = resolve(host="api.newsbreak.com", path="/Website/channel/news-list")
        assert r.deployment.app_label == "server-feed"
        assert r.is_confident

    def test_unknown_app_label_does_not_fall_back(self):
        r = resolve(app_label="billing-svc")
        assert r.deployment is None
        assert "billing-svc" in r.note

    def test_nothing_to_go_on(self):
        assert resolve().confidence == "unresolved"

    def test_legacy_helper_still_returns_a_deployment(self):
        assert resolve_deployment(
            host="api.newsbreak.com", path="/Website/channel/x"
        ).app_label == "server-feed"


class TestUnconfirmedAttributionIsVisible:
    def test_prompt_marks_a_guessed_workload(self, sample_grafana):
        identity = AlertIdentity(alert_name="large-scale-5xx", labels={"host": "api.newsbreak.com"})
        resolution = resolve(host="api.newsbreak.com")
        prompt = diagnose.build_prompt(
            identity, None, resolution, [], [], [], []
        )
        assert "UNCONFIRMED" in prompt

    def test_prompt_says_so_when_nothing_resolved(self):
        identity = AlertIdentity(alert_name="unknown", labels={})
        resolution = resolve(host="foo.example.com")
        prompt = diagnose.build_prompt(identity, None, resolution, [], [], [], [])
        assert "Unresolved" in prompt

    def test_exact_match_carries_no_warning(self):
        identity = AlertIdentity(alert_name="news-list-for-channel-p99", labels={})
        resolution = resolve(host="api.newsbreak.com", path="/Website/channel/x")
        prompt = diagnose.build_prompt(identity, None, resolution, [], [], [], [])
        assert "UNCONFIRMED" not in prompt

    def test_reply_warns_on_a_guessed_workload(self):
        from oncall_agent.pipeline import format_reply

        result = TriageResult(
            identity=AlertIdentity(alert_name="large-scale-5xx"),
            resolution=resolve(host="api.newsbreak.com"),
        )
        assert "attribution unconfirmed" in format_reply(result).lower()

    def test_metric_caveat_rides_with_the_number(self, sample_grafana):
        identity = AlertIdentity(alert_name="large-scale-5xx", labels={"host": "api.newsbreak.com"})
        resolution = resolve(host="api.newsbreak.com")
        metrics = evidence.gather(sample_grafana, identity, None, resolution)
        workload = [m for m in metrics if "kube_" in m.query]
        assert workload, "workload probes should still run on a guess"
        assert all(m.caveat for m in workload)
        assert "⚠" in workload[0].summarize()


class TestUnresolvedStillInvestigates:
    """§1.1: an unresolved deployment drops workload-scoped probes, not the whole pack."""

    def test_named_app_is_queried_even_when_not_in_the_table(self, sample_grafana):
        identity = AlertIdentity(alert_name="unknown", labels={"app": "billing-svc"})
        metrics = evidence.gather(sample_grafana, identity, None, resolve(app_label="billing-svc"))
        assert metrics, "a named workload must still be probed"
        assert any("billing-svc" in m.query for m in metrics)
        assert all(m.caveat for m in metrics if "billing-svc" in m.query)


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


class FakeLLM:
    """Replays a fixed action sequence, so loop control can be tested without a model."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0
        self.settings = Settings()

    def generate_json(self, prompt, schema, *, deep=False, system=None):
        self.calls += 1
        return self.actions.pop(0) if self.actions else {"tool": "conclude", "reasoning": "done"}


class TestInvestigationLoop:
    """Budgets and guards are enforced in the loop, where the model has no vote."""

    def _registry(self):
        from oncall_agent.repos import Repo, RepoRegistry
        from pathlib import Path

        return RepoRegistry(repos=[Repo(name="server", path=Path("/tmp"))])

    def test_round_limit_is_enforced(self):
        from oncall_agent.investigate.loop import investigate

        llm = FakeLLM([{"tool": "list_dir", "repo": "server", "reasoning": "look"}] * 20)
        tools = {"list_dir": lambda **kw: type("R", (), {"text": "ok"})()}

        inv = investigate(llm, tools, self._registry(), "ctx", max_rounds=3)
        assert inv.rounds == 3
        assert "round limit" in inv.stopped_because

    def test_conclude_stops_the_loop(self):
        from oncall_agent.investigate.loop import investigate

        llm = FakeLLM([
            {"tool": "list_dir", "repo": "server", "reasoning": "look"},
            {"tool": "conclude", "finding": "found it", "reasoning": "done"},
        ])
        tools = {"list_dir": lambda **kw: type("R", (), {"text": "ok"})()}

        inv = investigate(llm, tools, self._registry(), "ctx", max_rounds=6)
        assert inv.rounds == 1
        assert inv.finding == "found it"

    def test_missing_required_arg_is_reported_not_retried_blindly(self):
        from oncall_agent.investigate.loop import investigate

        # A bare "tool error" leaves the model to guess, and it guesses by reissuing
        # the same broken call until the budget is gone.
        llm = FakeLLM([{"tool": "search_code", "reasoning": "search"}])
        called = []
        tools = {"search_code": lambda **kw: called.append(kw) or type("R", (), {"text": "x"})()}

        inv = investigate(llm, tools, self._registry(), "ctx", max_rounds=2)
        assert called == [], "tool must not run without its required argument"
        assert "requires pattern" in inv.steps[0].observation

    def test_repeated_call_is_short_circuited(self):
        from oncall_agent.investigate.loop import investigate

        action = {"tool": "list_dir", "repo": "server", "reasoning": "look"}
        llm = FakeLLM([dict(action), dict(action)])
        calls = []
        tools = {"list_dir": lambda **kw: calls.append(kw) or type("R", (), {"text": "ok"})()}

        inv = investigate(llm, tools, self._registry(), "ctx", max_rounds=4)
        assert len(calls) == 1, "an identical call cannot yield a new observation"
        assert "Already ran" in inv.steps[1].observation

    def test_model_failure_stops_cleanly(self):
        from oncall_agent.investigate.loop import investigate
        from oncall_agent.llm import LLMUnavailable

        class Failing(FakeLLM):
            def generate_json(self, *a, **kw):
                raise LLMUnavailable("overloaded")

        inv = investigate(Failing([]), {}, self._registry(), "ctx", max_rounds=3)
        assert inv.rounds == 0
        assert "model unavailable" in inv.stopped_because

    def test_observations_are_truncated(self):
        from oncall_agent.investigate.loop import MAX_OBSERVATION_CHARS, investigate

        llm = FakeLLM([{"tool": "list_dir", "repo": "server", "reasoning": "look"}])
        tools = {"list_dir": lambda **kw: type("R", (), {"text": "x" * 99_000})()}

        inv = investigate(llm, tools, self._registry(), "ctx", max_rounds=2)
        assert len(inv.steps[0].observation) < MAX_OBSERVATION_CHARS + 100


class TestRepoRegistry:
    def test_owning_repo_is_ranked_first(self):
        from oncall_agent.repos import Repo, RepoRegistry
        from pathlib import Path

        registry = RepoRegistry(repos=[
            Repo(name="other", path=Path("/tmp")),
            Repo(name="server", path=Path("/tmp"), owns_services=["server-feed"]),
        ])
        assert registry.rank_for("server-feed")[0].name == "server"

    def test_unowned_repos_are_ranked_not_removed(self):
        from oncall_agent.repos import Repo, RepoRegistry
        from pathlib import Path

        registry = RepoRegistry(repos=[
            Repo(name="other", path=Path("/tmp")),
            Repo(name="server", path=Path("/tmp"), owns_services=["server-feed"]),
        ])
        # Cross-repo causes are the hard part of multi-repo triage; excluding a repo
        # outright would hide them.
        assert len(registry.rank_for("server-feed")) == 2


class TestPartialFindings:
    """Rounds already completed found real things.

    Discarding them because a later call failed throws away work the engineer can use,
    and an incident is exactly when partial information still helps.
    """

    def _registry(self):
        from oncall_agent.repos import Repo, RepoRegistry
        from pathlib import Path

        return RepoRegistry(repos=[Repo(name="server", path=Path("/tmp"))])

    def test_model_failure_keeps_what_was_found(self):
        from oncall_agent.investigate.loop import investigate
        from oncall_agent.llm import LLMUnavailable

        class FailsAfterOne(FakeLLM):
            def generate_json(self, *a, **kw):
                self.calls += 1
                if self.calls > 1:
                    raise LLMUnavailable("overloaded")
                return {"tool": "search_code", "pattern": "ErrCode", "reasoning": "look"}

        tools = {"search_code": lambda **kw: type("R", (), {"text": "3 matches in x.go"})()}
        inv = investigate(FailsAfterOne([]), tools, self._registry(), "ctx", max_rounds=4)

        assert inv.rounds == 1
        assert inv.finding, "a completed round must survive a later failure"
        assert "search_code" in inv.finding

    def test_round_limit_keeps_what_was_found(self):
        from oncall_agent.investigate.loop import investigate

        llm = FakeLLM([{"tool": "search_code", "pattern": "x", "reasoning": "look"}] * 10)
        tools = {"search_code": lambda **kw: type("R", (), {"text": "hit"})()}

        inv = investigate(llm, tools, self._registry(), "ctx", max_rounds=2)
        assert "round limit" in inv.stopped_because
        assert inv.finding

    def test_partial_finding_does_not_invent_a_cause(self):
        from oncall_agent.investigate.loop import investigate
        from oncall_agent.llm import LLMUnavailable

        class Failing(FakeLLM):
            def generate_json(self, *a, **kw):
                self.calls += 1
                if self.calls > 1:
                    raise LLMUnavailable("overloaded")
                return {"tool": "search_code", "pattern": "x", "reasoning": "look"}

        tools = {"search_code": lambda **kw: type("R", (), {"text": "no matches"})()}
        inv = investigate(Failing([]), tools, self._registry(), "ctx", max_rounds=4)

        # Stopping early means it did not get there; a cause stated here would be made up.
        assert "did not reach a conclusion" in inv.finding

    def test_explicit_conclusion_is_not_overwritten(self):
        from oncall_agent.investigate.loop import investigate

        llm = FakeLLM([{"tool": "conclude", "finding": "root cause is X", "reasoning": "done"}])
        inv = investigate(llm, {}, self._registry(), "ctx", max_rounds=4)
        assert inv.finding == "root cause is X"


class TestThreadMemory:
    """A Slack thread is a conversation.

    The second mention is almost always a follow-up, and an agent that re-derives
    everything each time wastes the engineer's time and its own budget. Measured: a
    follow-up went from 5 rounds to 1 once the earlier turn was in context.
    """

    def test_first_turn_has_nothing_to_build_on(self):
        from oncall_agent.memory import summarize

        assert summarize([]) == ""

    def test_prior_conclusion_is_carried_forward(self):
        from oncall_agent.memory import summarize

        text = summarize([{
            "alert_name": "get-empty-docids",
            "confidence": "low",
            "diagnosis": {"likely_cause": "ranking recall returned nothing"},
            "steps": [],
        }])
        assert "ranking recall returned nothing" in text
        assert "low confidence" in text

    def test_what_was_searched_is_carried_too(self):
        """Repeating a search that already came back empty is the usual way a
        follow-up wastes a round."""
        from oncall_agent.memory import summarize

        text = summarize([{
            "alert_name": "get-empty-docids",
            "diagnosis": None,
            "steps": [
                {"tool": "search_code", "args": {"pattern": "docid"},
                 "observation": "5 matches in 2 files"},
            ],
        }])
        assert "search_code docid" in text
        assert "already checked" in text

    def test_prior_steps_are_bounded(self):
        from oncall_agent.memory import MAX_PRIOR_STEPS, summarize

        text = summarize([{
            "alert_name": "a",
            "diagnosis": None,
            "steps": [
                {"tool": "search_code", "args": {"pattern": f"p{i}"}, "observation": "x"}
                for i in range(30)
            ],
        }])
        assert text.count("search_code") <= MAX_PRIOR_STEPS
