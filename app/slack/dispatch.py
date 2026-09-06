"""把两种 Agent 收敛成一条事件流。

Slack 没有导航栏，所以入口必须统一——用户只会 @ 一句话，不会先点「运维」还是「问答」。
但入口统一不等于执行统一：一个知识问题不需要 Plan-Execute-Replan 跑四次模型调用，
而一次告警排查也不该退化成一问一答。

所以这里做的是翻译，不是合并：路由（router.py）在图外面把 turn 定好，这里按 turn 分派到
运维 Agent 或对话 Agent，再把两者格式不同的事件规整成同一种形状交给 handlers。
判断留在外面，是因为「这次不用排查」这个结论一旦能由执行层自己下，就没人看得见它下错了。
"""

from collections.abc import AsyncIterator
from typing import Any

from loguru import logger

from app.knowledge.cases import cases_for_alert, render
from app.slack.alerts import alert_services, match_alert

TRIAGE_TASK = """请排查下面这条生产告警，并给出诊断报告。

{alert}
{question}
要求：
- 先用工具取证，再下结论；没有查到的就说没查到，不要编造数据
- 空结果代表「没有数据」，不代表「系统健康」，两者要区分开
- 说明这个服务是故障源头，还是上游故障的受害者
- 每条结论都要指出它依据的是哪一步查到的什么"""


def _history(alert_text: str) -> str:
    """告警名和服务名是已知事实，不需要判断，所以历史案例自动注入而不是等 agent 想起来查。
    模糊的那一半（「这个症状像什么」）留给 search_incident_cases 工具。"""
    name = match_alert(alert_text)
    if not name:
        return ""
    try:
        hits = cases_for_alert(name, alert_services(name))
    except Exception as exc:
        logger.warning(f"历史案例检索失败: {exc}")
        return ""
    if not hits:
        return ""
    logger.info(f"告警 {name!r} 命中 {len(hits)} 篇历史案例")
    body = render(hits, "## 这条告警的历史案例")
    return (
        f"\n{body}\n\n"
        "以上是本系统过去的诊断结论，属于线索不是结论——本次仍需重新取证；"
        "如果这次的证据和它们冲突，以本次证据为准并说明冲突。\n"
    )


def _triage_input(alert_text: str, question: str) -> str:
    asked = f"\n工程师另外问了：{question}\n" if question else "\n"
    task = TRIAGE_TASK.format(alert=alert_text or "(thread 里没有取到告警原文)", question=asked)
    return task + _history(alert_text)


async def run_turn(
    turn: str,
    *,
    question: str,
    alert_text: str,
    thread_id: str,
    confirmed_by: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """按 turn 分派，产出 {type, message?, response?} 事件。"""
    if turn == "chat":
        async for out in _chat(question, thread_id):
            yield out
    elif turn == "writeup":
        async for out in _writeup(alert_text, thread_id, confirmed_by):
            yield out
    else:
        async for out in _triage(question, alert_text, thread_id):
            yield out


async def _triage(question: str, alert_text: str, thread_id: str) -> AsyncIterator[dict[str, Any]]:
    from app.services.aiops_service import aiops_service

    async for event in aiops_service.execute(
        _triage_input(alert_text, question), session_id=thread_id
    ):
        kind = event.get("type")
        if kind == "complete":
            yield {"type": "complete", "response": event.get("response", "")}
        elif kind == "error":
            yield {"type": "error", "message": event.get("message", "unknown failure")}
        elif event.get("message"):
            yield {"type": "progress", "message": event["message"]}


async def _chat(question: str, thread_id: str) -> AsyncIterator[dict[str, Any]]:
    from app.services.rag_agent_service import rag_agent_service

    tools_used: list[str] = []
    chunks: list[str] = []
    try:
        async for event in rag_agent_service.query_stream(question, session_id=thread_id):
            kind = event.get("type")
            if kind == "content":
                chunks.append(str(event.get("data", "")))
            elif kind == "tool_call":
                name = str(event.get("data", "")) or "tool"
                tools_used.append(name)
                # 对话也会查东西；不说的话，等待期间看起来就是 bot 卡住了。
                yield {"type": "progress", "message": f"查询 {name}"}
            elif kind == "error":
                yield {"type": "error", "message": str(event.get("data", "chat failed"))}
                return
    except Exception as exc:
        logger.exception("chat turn failed")
        yield {"type": "error", "message": str(exc)}
        return

    yield {"type": "complete", "response": "".join(chunks).strip()}


async def _writeup(
    alert_text: str, thread_id: str, confirmed_by: str | None
) -> AsyncIterator[dict[str, Any]]:
    """把这一轮排查固化成案例。只由显式操作触发，产出是 PR 不是既成事实。"""
    from app.knowledge.writeback import draft_case, open_case_pr
    from app.services.aiops_service import aiops_service

    report = aiops_service.last_response(thread_id)
    if not report:
        yield {"type": "complete", "response": "这个 thread 里还没有排查结论，没有可以记录的东西。"}
        return

    yield {"type": "progress", "message": "起草案例"}
    name = match_alert(alert_text) or ""
    draft = await draft_case(alert=name, services=alert_services(name), report=report)
    if draft is None:
        yield {"type": "error", "message": "案例起草失败"}
        return

    yield {"type": "progress", "message": f"提交 {draft.filename}"}
    url = open_case_pr(draft, confirmed_by=confirmed_by)
    if not url:
        yield {"type": "complete", "response": f"案例已起草（{draft.filename}），但没能开 PR，请查日志。"}
        return

    verb = "更新" if draft.replaces_existing else "新增"
    yield {"type": "complete", "response": f"已{verb} `{draft.filename}` 并开了 PR：{url}"}
