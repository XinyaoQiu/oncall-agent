"""告警名识别 —— 路由的最后一条规则，只服务一种情况：人手动把告警贴进来。

正常路径不靠这个。一条告警是不是告警，由 Slack 的消息来源决定（`bot_id` / `app_id` /
频道），那是元数据，不会看错；关键词表会。所以这里认不出来不代表不排查——它只是让
「工程师复制粘贴一段告警文本」也能走进排查流程。

知识库里每篇 runbook 的 `**告警名**:` 就是这张表的来源，避免两处各写一份。
"""

import re
from functools import lru_cache
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parents[2] / "aiops-docs"

_NAME_RE = re.compile(r"\*\*告警名\*\*:\s*`([^`]+)`")

# 告警文本未必带告警名，但这些词几乎只出现在告警里。
FIRING_MARKERS = ("[firing]", "[resolved]", "alertmanager", "告警触发", "severity=")


@lru_cache
def known_alert_names() -> tuple[str, ...]:
    names: list[str] = []
    if DOCS_DIR.is_dir():
        for doc in sorted(DOCS_DIR.glob("*.md")):
            names.extend(_NAME_RE.findall(doc.read_text(encoding="utf-8")))
    return tuple(dict.fromkeys(names))


def match_alert(text: str | None) -> str | None:
    """文本里像不像一条告警。返回命中的告警名，或 firing 标记，或 None。"""
    if not text:
        return None
    lowered = text.lower()

    for name in known_alert_names():
        if name.lower() in lowered:
            return name

    for marker in FIRING_MARKERS:
        if marker in lowered:
            return marker

    return None
