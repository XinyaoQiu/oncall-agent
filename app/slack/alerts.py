"""告警名识别 —— 路由的最后一条规则，只服务一种情况：人手动把告警贴进来。

正常路径不靠这个。一条告警是不是告警，由 Slack 的消息来源决定（`bot_id` / `app_id` /
频道），那是元数据，不会看错；关键词表会。所以这里认不出来不代表不排查——它只是让
「工程师复制粘贴一段告警文本」也能走进排查流程（spec §5.1 规则 5）。

表本身在 `rec-knowledge/alerts.yaml`。以前是从 `aiops-docs/*.md` 正则抓 `**告警名**:`，
那等于把告警清单藏在文档格式里——文档换个写法就静默失效，而现在文档归公司 wiki 管，
我们没法要求它保持某种写法。
"""

from app.config import Settings
from app.knowledge.registry import AlertEntry, alert_entries, known_alert_names, lookup

# 告警文本未必带告警名，但这些词几乎只出现在告警里。
FIRING_MARKERS = ("[firing]", "[resolved]", "alertmanager", "告警触发", "severity=")

__all__ = ["FIRING_MARKERS", "known_alert_names", "match_alert", "alert_services"]


def match_alert(text: str | None, settings: Settings | None = None) -> str | None:
    """文本里像不像一条告警。返回命中的告警名，或 firing 标记，或 None。"""
    if not text:
        return None
    lowered = text.lower()

    for entry in alert_entries(settings):
        hit = entry.matches(lowered)
        if hit:
            return hit

    for marker in FIRING_MARKERS:
        if marker in lowered:
            return marker

    return None


def alert_services(name: str | None, settings: Settings | None = None) -> tuple[str, ...]:
    """登记过的告警关联哪些服务；供案例精确检索用。"""
    entry: AlertEntry | None = lookup(name, settings) if name else None
    return entry.services if entry else ()
