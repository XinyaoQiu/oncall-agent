"""历史案例的检索。

两种模式，触发方式刻意不同：

- **精确**（`cases_for_alert`）：告警名和服务名是从告警本身来的已知事实，没有判断成分。
  做成工具调用只会多一个「agent 忘了调」的失败模式，所以它自动注入，不给 agent 选择。
- **语义**（`search_cases`）：「这个症状让我想起什么」需要先组织查询词，那才是判断。
  做成工具，让 executor 在排查过程中**每冒出一个新实体就查一次**——字符串是查出来的，
  不是一开始就有的。

用 grep 不用向量：语料小，而且案例是 agent 自己按同一套词汇写的，两端都受控。
"""

import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.config import Settings, get_settings
from app.knowledge.paths import cases_dir

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_LIST_RE = re.compile(r"\[(.*?)\]")
SNIPPET_CHARS = 400


@dataclass(frozen=True)
class Case:
    path: Path
    alert: str
    services: tuple[str, ...]
    date: str
    body: str

    @property
    def name(self) -> str:
        return self.path.stem

    def snippet(self, chars: int = SNIPPET_CHARS) -> str:
        text = self.body.strip()
        return text if len(text) <= chars else text[:chars].rstrip() + " …"


def _parse(path: Path) -> Case | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(f"读取案例失败 {path}: {exc}")
        return None

    alert, services, date = "", (), ""
    match = _FM_RE.match(raw)
    body = raw[match.end() :] if match else raw
    for line in (match.group(1).splitlines() if match else []):
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "alert":
            alert = value
        elif key == "date":
            date = value
        elif key == "services":
            inner = _LIST_RE.search(value)
            items = (inner.group(1) if inner else value).split(",")
            services = tuple(s.strip() for s in items if s.strip())
    return Case(path=path, alert=alert, services=services, date=date, body=body)


def load_cases(settings: Settings | None = None) -> list[Case]:
    directory = cases_dir(settings)
    if not directory.is_dir():
        logger.warning(f"案例目录不存在: {directory}")
        return []
    cases = [_parse(p) for p in sorted(directory.glob("*.md"))]
    return [c for c in cases if c is not None]


def cases_for_alert(
    alert: str | None,
    services: tuple[str, ...] = (),
    settings: Settings | None = None,
) -> list[Case]:
    """按告警名 / 服务名精确命中。两者都空时返回空，不做模糊兜底。"""
    alert_l = (alert or "").strip().lower()
    wanted = {s.strip().lower() for s in services if s.strip()}
    if not alert_l and not wanted:
        return []

    hits = []
    for case in load_cases(settings):
        if alert_l and case.alert.lower() == alert_l:
            hits.append(case)
        elif wanted and wanted & {s.lower() for s in case.services}:
            hits.append(case)
    hits.sort(key=lambda c: c.date, reverse=True)
    return hits


def _terms(query: str) -> list[str]:
    parts = re.split(r"[\s,，、;；]+", query or "")
    return [p.strip().lower() for p in parts if len(p.strip()) >= 2]


def search_cases(
    query: str,
    settings: Settings | None = None,
    limit: int | None = None,
) -> list[tuple[Case, list[str]]]:
    """按词命中，返回 (案例, 命中的词)，命中词多的排前面。"""
    settings = settings or get_settings()
    terms = _terms(query)
    if not terms:
        return []

    scored = []
    for case in load_cases(settings):
        haystack = f"{case.alert} {' '.join(case.services)} {case.body}".lower()
        matched = [t for t in terms if t in haystack]
        if matched:
            scored.append((case, matched))

    scored.sort(key=lambda item: (len(item[1]), item[0].date), reverse=True)
    return scored[: (limit or settings.case_search_max_hits)]


def render(cases: list[Case], heading: str) -> str:
    if not cases:
        return ""
    parts = [heading]
    for case in cases:
        head = f"### {case.name}"
        meta = " · ".join(x for x in (case.alert, ", ".join(case.services)) if x)
        parts.append(f"{head}\n{meta}\n\n{case.snippet()}" if meta else f"{head}\n\n{case.snippet()}")
    return "\n\n".join(parts)
