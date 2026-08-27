"""The fact layer: no I/O, no model, no excuses for a guess that hides."""

from pathlib import Path

from app.domain.alerts import KNOWN_ALERTS, benign_checks, get_alert, match_alert
from app.domain.deployments import (
    DEPLOYMENTS,
    Deployment,
    deployment_for,
    resolve,
    resolve_blast_radius,
)
from app.domain.repos import Repo, RepoRegistry, load_registry

CHANNEL_ALERT = """[FIRING] news-list-for-channel p99 latency
app=server-feed path=/Website/channel/news-list-for-channel
value: 3.2s (threshold 2s)"""

FIVEXX_ALERT = """[FIRING] High 5xx error rate
host=www.newsbreak.com
value: 18% (threshold 5%)"""


class TestConfigLoads:
    def test_shard_table_is_not_empty(self):
        assert {d.app_label for d in DEPLOYMENTS} >= {
            "server-default",
            "server-feed",
            "server-a4api-web",
            "server-a4api-default",
        }

    def test_deployment_for_returns_the_record(self):
        d = deployment_for("server-feed")
        assert isinstance(d, Deployment)
        assert d.pod_pattern == "server-feed-*"
        assert d.replicas == 60

    def test_deployment_for_unknown_is_none(self):
        assert deployment_for("billing-svc") is None
        assert deployment_for(None) is None


class TestResolutionConfidence:
    """A guess must never be returned looking like a match.

    The failure this guards against is silent: the catch-all returns a real deployment, its
    pod and replica queries succeed, and the pack describes the wrong workload in exactly
    the format it would use for the right one.
    """

    def test_app_label_match_is_exact(self):
        r = resolve(app_label="server-feed")
        assert r.app_label == "server-feed"
        assert r.confidence == "exact"
        assert r.matched_by == "app_label"
        assert r.is_confident
        assert r.pod_pattern == "server-feed-*"
        assert r.replicas == 60

    def test_unknown_app_label_does_not_fall_back(self):
        r = resolve(app_label="billing-svc")
        assert r.app_label is None
        assert r.confidence == "unresolved"
        assert not r.is_confident
        assert "billing-svc" in r.note

    def test_unknown_host_resolves_to_nothing(self):
        r = resolve(host="foo.example.com")
        assert r.app_label is None
        assert r.confidence == "unresolved"
        assert "foo.example.com" in r.note

    def test_unknown_host_with_path_resolves_to_nothing(self):
        r = resolve(host="foo.example.com", path="/x")
        assert r.app_label is None
        assert r.confidence == "unresolved"

    def test_real_prefix_match_is_exact(self):
        r = resolve(host="api.newsbreak.com", path="/Website/channel/news-list")
        assert r.app_label == "server-feed"
        assert r.confidence == "exact"
        assert r.matched_by == "host+path"
        assert r.is_confident

    def test_longest_path_prefix_wins(self):
        # Both server-default ("/") and server-feed match; the more specific one wins.
        assert resolve(host="api.newsbreak.com", path="/Website/news/x").app_label == "server-feed"

    def test_web_path_resolves_to_web_deployment(self):
        r = resolve(host="www.newsbreak.com", path="/Website/web/home")
        assert r.app_label == "server-a4api-web"
        assert r.is_confident

    def test_unknown_path_is_flagged_not_trusted(self):
        r = resolve(host="api.newsbreak.com", path="/api/v2/charge")
        # The catch-all still answers — dropping it would empty the pack — but it says so.
        assert r.app_label == "server-default"
        assert r.confidence == "low"
        assert r.matched_by == "catch-all"
        assert not r.is_confident
        assert r.note

    def test_host_without_path_is_a_guess(self):
        r = resolve(host="api.newsbreak.com")
        assert r.confidence == "low"
        assert r.matched_by == "host-only"
        assert r.note
        assert not r.is_confident

    def test_path_without_host_is_still_exact(self):
        r = resolve(path="/Website/profile/get-profile")
        assert r.app_label == "server-a4api-default"
        assert r.matched_by == "path"
        assert r.is_confident

    def test_path_matching_no_prefix_at_all(self):
        # Without a host the "/" catch-all is in scope, so this exercises the low branch.
        r = resolve(path="/api/v2/charge")
        assert r.confidence == "low"
        assert r.matched_by == "catch-all"

    def test_nothing_to_go_on(self):
        r = resolve()
        assert r.confidence == "unresolved"
        assert r.matched_by == "nothing"
        assert r.app_label is None


class TestBlastRadius:
    def test_blast_radius_is_the_inverse(self):
        assert [d.app_label for d in resolve_blast_radius("server/router/feed.go")] == [
            "server-feed"
        ]

    def test_controller_directory_matches_by_prefix(self):
        assert [d.app_label for d in resolve_blast_radius("server/controller/feed/list.go")] == [
            "server-feed"
        ]

    def test_unrelated_code_has_no_blast_radius(self):
        assert resolve_blast_radius("scripts/backfill.py") == []


class TestAlerts:
    def test_alerts_load(self):
        assert {a.name for a in KNOWN_ALERTS} >= {
            "news-list-for-channel-p99",
            "feed-channel-empty",
            "get-empty-docids",
            "large-scale-5xx",
        }

    def test_matches_known_alert_by_keyword(self):
        assert match_alert(CHANNEL_ALERT).name == "news-list-for-channel-p99"

    def test_five_xx_alert_matches(self):
        assert match_alert(FIVEXX_ALERT).name == "large-scale-5xx"

    def test_highest_keyword_hit_count_wins(self):
        text = "5xx error rate spike, also mentions channel latency once"
        assert match_alert(text).name == "large-scale-5xx"

    def test_unknown_text_matches_nothing(self):
        assert match_alert("something entirely unfamiliar") is None

    def test_matching_is_case_insensitive(self):
        assert match_alert("CHANNEL LATENCY is high").name == "news-list-for-channel-p99"

    def test_get_alert(self):
        assert get_alert("get-empty-docids").fallback_expr
        assert get_alert("nope") is None
        assert get_alert(None) is None

    def test_benign_checks_render_name_description_and_how(self):
        checks = benign_checks("news-list-for-channel-p99")
        assert any("cold start" in c for c in checks)
        assert any("check: kube_pod_start_time" in c for c in checks)

    def test_benign_checks_for_unknown_alert_is_empty(self):
        assert benign_checks("unknown") == []


def _registry(tmp_path: Path) -> RepoRegistry:
    for name in ("server", "sre-configs", "rec-knowledge"):
        (tmp_path / name).mkdir()
    return RepoRegistry(
        repos=[
            Repo(name="server", path=tmp_path / "server", owns_services=["server-feed"]),
            Repo(name="sre-configs", path=tmp_path / "sre-configs", language="yaml"),
            Repo(name="rec-knowledge", path=tmp_path / "rec-knowledge", language="markdown"),
            Repo(name="missing", path=tmp_path / "missing"),
        ]
    )


class TestRepos:
    def test_available_is_directory_existence(self, tmp_path):
        reg = _registry(tmp_path)
        assert [r.name for r in reg.available()] == ["server", "sre-configs", "rec-knowledge"]
        assert reg.get("missing") is not None
        assert reg.get("missing").available is False
        assert reg.get("nope") is None

    def test_rank_for_orders_and_never_filters(self, tmp_path):
        reg = _registry(tmp_path)
        ranked = reg.rank_for("server-feed")
        assert ranked[0].name == "server"
        assert {r.name for r in ranked} == {r.name for r in reg.available()}

    def test_rank_for_keeps_repos_owning_nothing(self, tmp_path):
        reg = _registry(tmp_path)
        ranked = reg.rank_for("billing-svc")
        # Nothing owns it, so nothing is preferred — and nothing is dropped either.
        assert len(ranked) == len(reg.available())

    def test_rank_for_without_a_service(self, tmp_path):
        reg = _registry(tmp_path)
        assert [r.name for r in reg.rank_for(None)] == [r.name for r in reg.available()]

    def test_describe_lists_available_repos(self, tmp_path):
        described = _registry(tmp_path).describe()
        assert "- server:" in described
        assert "serves server-feed" in described
        assert "missing" not in described

    def test_describe_empty_registry(self):
        assert RepoRegistry().describe() == "(no repositories configured)"

    def test_repo_root_is_expanded(self, tmp_path):
        reg = load_registry(root=tmp_path)
        assert reg.get("server").path == tmp_path / "server"
        assert "${REPO_ROOT}" not in str(reg.get("api-schema").path)

    def test_excludes_come_from_config(self, tmp_path):
        server = load_registry(root=tmp_path).get("server")
        assert "vendor" in server.exclude_dirs
        assert "*.pb.go" in server.exclude_globs
