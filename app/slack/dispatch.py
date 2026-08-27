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

TRIAGE_TASK = """请排查下面这条生产告警，并给出诊断报告。

{alert}
{question}
要求：
- 先用工具取证，再下结论；没有查到的就说没查到，不要编造数据
- 空结果代表「没有数据」，不代表「系统健康」，两者要区分开
- 说明这个服务是故障源头，还是上游故障的受害者
- 每条结论都要指出它依据的是哪一步查到的什么"""


def _triage_input(alert_text: str, question: str) -> str:
    asked = f"\n工程师另外问了：{question}\n" if question else "\n"
    return TRIAGE_TASK.format(alert=alert_text or "(thread 里没有取到告警原文)", question=asked)


async def run_turn(
    turn: str,
    *,
    question: str,
    alert_text: str,
    thread_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """按 turn 分派，产出 {type, message?, response?} 事件。"""
    if turn == "chat":
        async for out in _chat(question, thread_id):
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
