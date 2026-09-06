"""把一次排查固化成案例。

写入纪律，每一条都是刻意的：

- **必须显式触发。** 消息里出现「记一下」不该产生 commit——靠词触发副作用是被删掉的老做法。
- **只开 PR，绝不推 main。** merge 那个动作本身就是确认：谁批的、什么时候，git 都记着。
- **只碰 cases/。** 越权改别的目录，人就没法闭着眼 merge 了。
- **工作区不干净就拒绝。** 否则会把无关改动一起卷进这个 PR。

同一告警同一根因复发时，改已有案例加一个日期，而不是新开一篇——复发次数本身就是该去做
根治的信号，拆成八篇近似案例反而把这个信号摊平了，还会在检索时占满 top-k。
"""

import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from loguru import logger

from app.config import Settings, get_settings
from app.knowledge.cases import cases_for_alert, render
from app.knowledge.paths import cases_dir, rec_knowledge_root

DRAFT_PROMPT = """你在把一次线上排查固化成一篇案例，供下次同类告警检索到。

写成 markdown，开头是 YAML frontmatter：

---
date: {today}
alert: {alert}
services: [{services}]
severity: <P1 | P2 | benign>
last_verified: {today}
---

正文不要套模板、不要填空。**没有的内容就不写**，编造比缺失严重得多。
按需要覆盖下面这些，顺序和标题自己定：

- **症状**：可观测到的表象——错误码、报错字符串、指标形态、影响面。
  这是下次检索的入口：下一个人搜的是他看得见的东西（"memcache 报错"、"502"），
  不是根因（"ingress reload"）。所以症状必须写进去，写不全就等于以后搜不到。
- **根因**：查到哪一步、靠什么证据坐实的。没坐实就写"未坐实"。
- **影响**：量化数字，注明口径。
- **迷惑点**：看起来像 X 其实不是——下次犯同样误判的人搜的正是 X。
- **决定性动作**：哪一步真正区分开了可能性。

已有的相关案例：
{existing}

如果这次和上面某一篇是**同一个根因**，不要新写一篇：输出那篇的完整更新版，
在时间线上加这次的日期，并把新增信息并进去。拿不准是不是同一个根因就新写一篇——
合并会覆盖掉区分性的细节，不可逆；多一篇只是冗余，以后还能再合。

第一行输出文件名（形如 `2026-08-27-short-slug.md`，复用已有案例就用它原来的文件名），
第二行开始是文件内容。"""


@dataclass(frozen=True)
class DraftedCase:
    filename: str
    content: str
    replaces_existing: bool


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=60)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "incident").lower()).strip("-")
    return (slug or "incident")[:48]


async def draft_case(
    *,
    alert: str,
    services: tuple[str, ...],
    report: str,
    settings: Settings | None = None,
) -> DraftedCase | None:
    """让模型起草一篇案例。失败返回 None，绝不抛。"""
    settings = settings or get_settings()
    today = date.today().isoformat()
    existing = cases_for_alert(alert, services, settings)
    existing_text = render(existing, "## 已有案例") if existing else "（没有）"

    try:
        from app.core.llm_factory import llm_factory

        llm = llm_factory.create_chat_model(
            model=settings.rag_model, temperature=0, streaming=False
        )
        prompt = DRAFT_PROMPT.format(
            today=today,
            alert=alert or "unknown",
            services=", ".join(services),
            existing=existing_text,
        )
        reply = await llm.ainvoke([("system", prompt), ("user", report[:20000])])
    except Exception as exc:
        logger.warning(f"案例起草失败: {exc}")
        return None

    text = (getattr(reply, "content", "") or "").strip()
    if not text:
        return None

    first, _, rest = text.partition("\n")
    filename = first.strip().strip("`").strip()
    if not filename.endswith(".md") or "/" in filename:
        filename = f"{today}-{_slugify(alert)}.md"
        rest = text
    known = {c.path.name for c in existing}
    return DraftedCase(filename=filename, content=rest.strip() + "\n", replaces_existing=filename in known)


def open_case_pr(
    draft: DraftedCase,
    *,
    confirmed_by: str | None,
    settings: Settings | None = None,
) -> str | None:
    """写文件、开分支、推、开 PR。返回 PR 链接或 None。"""
    settings = settings or get_settings()
    if not settings.case_writeback_enabled:
        logger.info("case_writeback_enabled=false，跳过回写")
        return None

    root = rec_knowledge_root(settings)
    if not (root / ".git").exists():
        logger.warning(f"{root} 不是 git 仓库，跳过回写")
        return None

    dirty = _run(["git", "status", "--porcelain"], root)
    if dirty.stdout.strip():
        # 工作区脏的时候提交会把无关改动一起卷进来，人就没法闭眼 merge 了。
        logger.warning("rec-knowledge 工作区不干净，拒绝回写")
        return None

    branch = f"{settings.case_writeback_branch_prefix}{Path(draft.filename).stem}"
    target = cases_dir(settings) / draft.filename
    verb = "update" if draft.replaces_existing else "record"

    base = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip() or "master"
    try:
        if _run(["git", "checkout", "-b", branch], root).returncode != 0:
            _run(["git", "checkout", branch], root)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(draft.content, encoding="utf-8")

        _run(["git", "add", str(target.relative_to(root))], root)
        _run(["git", "commit", "-m", f"feat: {verb} case {Path(draft.filename).stem}"], root)
        pushed = _run(["git", "push", "-u", "origin", branch], root)
        if pushed.returncode != 0:
            logger.warning(f"推送失败: {pushed.stderr.strip()[:200]}")
            return None

        review = (
            f"工程师 {confirmed_by} 在 thread 里确认过这个结论。"
            if confirmed_by
            else "**thread 里无人回应，这个结论没有经过人工确认——需要认真 review。**"
        )
        body = f"由 oncall-agent 起草。\n\n{review}\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
        pr = _run(
            ["gh", "pr", "create", "--base", base, "--head", branch,
             "--title", f"feat: {verb} case {Path(draft.filename).stem}", "--body", body],
            root,
        )
        if pr.returncode != 0:
            logger.warning(f"开 PR 失败: {pr.stderr.strip()[:200]}")
            return None
        url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else None
        logger.info(f"案例 PR 已创建: {url}")
        return url
    except Exception as exc:
        logger.warning(f"回写异常: {exc}")
        return None
    finally:
        _run(["git", "checkout", base], root)
