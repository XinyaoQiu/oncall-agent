"""知识层：注册表、案例检索、常驻上下文、回写闸门。

守的是几条容易悄悄退化的性质：告警名匹配不能误命中、精确检索不做模糊兜底、
常驻上下文超限要喊、回写默认关且工作区脏时拒绝。
"""

import pytest

from app.config import Settings
from app.knowledge import cases as case_mod
from app.knowledge import context as ctx_mod
from app.knowledge.registry import alert_entries, known_alert_names, lookup
from app.knowledge.writeback import DraftedCase, open_case_pr
from app.slack.alerts import alert_services, match_alert


def settings(**overrides) -> Settings:
    base = dict(slack_bot_token="xoxb-test", slack_app_token="xapp-test")
    base.update(overrides)
    return Settings(**base)


# --- 告警注册表 ---------------------------------------------------------------


def test_registry_loads_from_yaml_not_from_doc_formatting():
    names = known_alert_names()
    assert "HighCPUUsage" in names
    assert "feed channel empty count" in names


def test_a_known_alert_name_matches():
    assert match_alert("[FIRING] HighCPUUsage on node-3, cpu 94%") == "HighCPUUsage"


def test_an_unregistered_alert_does_not_match_by_name():
    """认不出不等于不排查——结构路由才是主力，这里只是最后一条规则。"""
    assert match_alert("[FIRING:1] QuoteServiceSaturation zone=euw1") is None


@pytest.mark.parametrize("text", ["zoom meeting room", "boom", "status 15021 returned"])
def test_short_ascii_aliases_do_not_match_inside_words(text):
    """'oom' 不能命中 'zoom'，'502' 不能命中 '15021'。"""
    assert match_alert(text) is None


def test_chinese_aliases_still_match_as_substrings():
    assert match_alert("告警：内存使用率过高") == "HighMemoryUsage"


def test_firing_marker_is_the_last_resort():
    assert match_alert("[FIRING] something we have never catalogued") == "[firing]"


def test_registry_entries_carry_the_doc_and_services():
    entry = lookup("news-list-for-channel p99 latency")
    assert entry is not None
    assert entry.doc == "runbook-channel-latency-deploy-warmup.md"
    assert "server-feed" in entry.services


def test_longer_names_win_over_shorter_aliases():
    """'server 5xx' 的别名 '5xx' 不能抢在更具体的告警前面。"""
    assert [e.name for e in alert_entries()] == sorted(
        (e.name for e in alert_entries()), key=len, reverse=True
    )


# --- 案例检索 -----------------------------------------------------------------


def test_cases_parse_frontmatter():
    cases = case_mod.load_cases()
    assert cases, "案例目录不该是空的"
    assert all(c.date for c in cases)


def test_exact_lookup_by_alert_name():
    hits = case_mod.cases_for_alert("web 502")
    assert any("ingress-webflow-dns-reload" in c.name for c in hits)


def test_exact_lookup_by_service_name():
    hits = case_mod.cases_for_alert(None, ("server-a4api-web",))
    assert any("a4api-web-5xx" in c.name for c in hits)


def test_exact_lookup_never_falls_back_to_fuzzy():
    """精确检索空手而归就该返回空，不能悄悄退化成模糊匹配。"""
    assert case_mod.cases_for_alert("NoSuchAlertEverRegistered") == []
    assert case_mod.cases_for_alert(None, ()) == []


def test_grep_ranks_the_case_matching_most_terms_first():
    """memcache + 502 + ingress 三个词都命中的那篇必须排第一。"""
    hits = case_mod.search_cases("memcache 502 ingress")
    assert hits
    top, matched = hits[0]
    assert "ingress-webflow-dns-reload" in top.name
    assert len(matched) == 3


def test_grep_ignores_one_character_noise():
    assert case_mod.search_cases("a") == []


def test_grep_returns_nothing_rather_than_guessing():
    assert case_mod.search_cases("kubernetes-operator-crd-webhook") == []


# --- 常驻上下文 ---------------------------------------------------------------


def test_datasource_context_is_present_and_has_no_frontmatter():
    text = ctx_mod.datasource_context()
    assert "lb_access_logs" in text
    assert not text.lstrip().startswith("---")


def test_resident_context_can_drop_lessons_for_the_executor():
    with_lessons = ctx_mod.resident_context()
    without = ctx_mod.resident_context(include_lessons=False)
    assert len(without) < len(with_lessons)


def test_resident_context_truncates_loudly_when_over_the_cap():
    """截断是止损不是解决方案，所以它必须留下痕迹。"""
    text = ctx_mod.resident_context(settings(resident_context_max_chars=500))
    assert text.endswith("[常驻上下文已截断]")


# --- 回写闸门 -----------------------------------------------------------------


def _draft() -> DraftedCase:
    return DraftedCase(filename="2026-01-01-x.md", content="body\n", replaces_existing=False)


def test_writeback_is_off_by_default():
    assert open_case_pr(_draft(), confirmed_by="U1", settings=settings()) is None


def test_writeback_refuses_when_the_working_tree_is_dirty(monkeypatch, tmp_path):
    """脏工作区提交会把无关改动卷进 PR，人就没法闭眼 merge 了。"""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("app.knowledge.writeback.rec_knowledge_root", lambda s=None: tmp_path)

    calls: list[list[str]] = []

    class Result:
        returncode, stdout, stderr = 0, " M cases/other.md\n", ""

    def fake_run(args, cwd):
        calls.append(args)
        return Result()

    monkeypatch.setattr("app.knowledge.writeback._run", fake_run)
    out = open_case_pr(_draft(), confirmed_by=None, settings=settings(case_writeback_enabled=True))

    assert out is None
    assert not any("commit" in a for a in calls), "拒绝之后不该还去 commit"


# --- 自动注入 -----------------------------------------------------------------


def test_triage_input_injects_history_for_a_registered_alert():
    """告警名是已知事实，不需要 agent 想起来去查。"""
    from app.slack.dispatch import _triage_input

    text = _triage_input("[FIRING] web 502 rate high on prod-fe", "")
    assert "这条告警的历史案例" in text
    assert "ingress-webflow-dns-reload" in text


def test_injected_history_is_labelled_as_a_lead_not_a_conclusion():
    """混着给，模型会把自己上次的猜测当事实读。"""
    from app.slack.dispatch import _triage_input

    text = _triage_input("[FIRING] web 502 on prod-fe", "")
    assert "仍需重新取证" in text


def test_triage_input_injects_nothing_for_an_unknown_alert():
    from app.slack.dispatch import _triage_input

    text = _triage_input("[FIRING:1] QuoteServiceSaturation zone=euw1", "")
    assert "历史案例" not in text


def test_registered_alert_exposes_its_services_for_lookup():
    assert "server-feed" in alert_services("feed channel empty count")
    assert alert_services("HighCPUUsage") == ()
