"""告警名注册表。

以前告警名是从 `aiops-docs/*.md` 里正则抓 `**告警名**:` 提取的。那种做法把「告警清单」
藏在了文档格式里——文档换个写法就静默失效，而 confluence 不归我们维护，没法要求它保持写法。

现在改成一张显式的表（`rec-knowledge/alerts.yaml`），几十行，遇到没登记的补一行。
"""

import re
from dataclasses import dataclass
from functools import lru_cache

import yaml
from loguru import logger

from app.config import Settings
from app.knowledge.paths import alerts_registry_path


_ASCII_WORD = re.compile(r"\A[a-z0-9][a-z0-9 _.-]*\Z")


def _contains(haystack: str, needle: str) -> bool:
    """ASCII 词按词边界匹配，否则 "oom" 会命中 "zoom"、"502" 会命中 "15021"。
    中文没有词边界，退回子串匹配。"""
    if _ASCII_WORD.match(needle):
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


@dataclass(frozen=True)
class AlertEntry:
    name: str
    doc: str | None = None
    services: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def matches(self, lowered: str) -> str | None:
        for needle in (self.name, *self.aliases):
            if needle and _contains(lowered, needle.lower()):
                return self.name
        return None


@lru_cache(maxsize=4)
def _load(path_str: str) -> tuple[AlertEntry, ...]:
    from pathlib import Path

    path = Path(path_str)
    if not path.is_file():
        logger.warning(f"告警注册表不存在: {path}")
        return ()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning(f"告警注册表解析失败: {exc}")
        return ()

    entries = []
    for item in raw.get("alerts") or []:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        entries.append(
            AlertEntry(
                name=name,
                doc=item.get("doc") or None,
                services=tuple(item.get("services") or ()),
                aliases=tuple(str(a) for a in (item.get("aliases") or ())),
            )
        )
    # 长名字优先，避免 "5xx" 抢在 "web 502" 前面命中
    entries.sort(key=lambda e: len(e.name), reverse=True)
    return tuple(entries)


def alert_entries(settings: Settings | None = None) -> tuple[AlertEntry, ...]:
    return _load(str(alerts_registry_path(settings)))


def known_alert_names(settings: Settings | None = None) -> tuple[str, ...]:
    return tuple(e.name for e in alert_entries(settings))


def lookup(name: str, settings: Settings | None = None) -> AlertEntry | None:
    for entry in alert_entries(settings):
        if entry.name.lower() == name.lower():
            return entry
    return None
