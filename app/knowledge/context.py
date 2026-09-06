"""常驻上下文：datasources/ 和 lessons/ 不参与检索，直接进提示词。

理由是这两类都是**通则**——「量化影响走 lb_access_logs 不走 server_logs」适用于每一次涉及
影响量化的排查。靠语义命中去召回它，就一定会在某次漏掉，而漏掉不会报错：数字照样算得出来，
只是一直是错的。

代价是有容量上限。超了就得合并条目，或者把某几条降级回检索——不能靠悄悄截断解决。
"""

from functools import lru_cache
from pathlib import Path

from loguru import logger

from app.config import Settings, get_settings
from app.knowledge.paths import datasources_dir, lessons_dir


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4 :].lstrip("\n") if end != -1 else text


def _read_dir(directory: Path) -> list[tuple[str, str]]:
    if not directory.is_dir():
        logger.warning(f"知识目录不存在: {directory}")
        return []
    out = []
    for doc in sorted(directory.glob("*.md")):
        if doc.name.upper() == "README.MD":
            continue
        body = _strip_frontmatter(doc.read_text(encoding="utf-8")).strip()
        if body:
            out.append((doc.stem, body))
    return out


def _render(sections: list[tuple[str, str]], heading: str) -> str:
    if not sections:
        return ""
    parts = [heading]
    parts += [f"### {name}\n\n{body}" for name, body in sections]
    return "\n\n".join(parts)


@lru_cache(maxsize=4)
def _datasources(root: str) -> str:
    return _render(
        _read_dir(Path(root)),
        "## 数据源目录（本系统实际能查到什么、在哪儿、怎么查）",
    )


@lru_cache(maxsize=4)
def _lessons(root: str) -> str:
    return _render(
        _read_dir(Path(root)),
        "## 经验教训（过去排查中沉淀，优先级高于通用文档）",
    )


def datasource_context(settings: Settings | None = None) -> str:
    return _datasources(str(datasources_dir(settings)))


def lesson_context(settings: Settings | None = None) -> str:
    return _lessons(str(lessons_dir(settings)))


def resident_context(settings: Settings | None = None, *, include_lessons: bool = True) -> str:
    """给 planner 的完整常驻上下文；executor 只要数据源那半。"""
    settings = settings or get_settings()
    blocks = [datasource_context(settings)]
    if include_lessons:
        blocks.append(lesson_context(settings))
    text = "\n\n".join(b for b in blocks if b)

    cap = settings.resident_context_max_chars
    if len(text) > cap:
        # 截断是止损不是解决：真超了要合并条目或把某几条降级回检索。
        logger.warning(f"常驻上下文 {len(text)} 字符超过上限 {cap}，已截断——该合并条目了")
        text = text[:cap] + "\n\n[常驻上下文已截断]"
    return text
