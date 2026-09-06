"""历史案例检索工具 —— 留给 agent 判断的那一半。

精确匹配（告警名 / 服务名）由 dispatch 自动注入，不经过这里。这个工具服务的是
「排查到一半冒出个新实体，去看看以前有没有见过」——查询词得先组织出来，那是判断。
"""

from langchain_core.tools import tool
from loguru import logger

from app.knowledge.cases import render, search_cases


@tool
def search_incident_cases(query: str) -> str:
    """检索本系统过去处理过的相似故障案例。

    排查过程中每发现一个新线索就调一次：服务名、组件名、错误码、异常字符串都可以作为查询词，
    多个词用空格分隔（如 "memcache 502 ingress"）。返回的是过去的诊断结论，属于参考，
    不能直接当作本次的结论——仍需重新取证。

    Args:
        query: 空格分隔的查询词

    Returns:
        命中的历史案例摘要；没有命中时明确说明
    """
    try:
        hits = search_cases(query)
    except Exception as exc:
        logger.warning(f"案例检索失败: {exc}")
        return f"案例检索失败: {exc}"

    if not hits:
        return f"没有命中历史案例（查询词: {query}）。这不代表没发生过，只代表这些词没匹配上。"

    logger.info(f"案例检索 '{query}' 命中 {len(hits)} 篇")
    cases = [case for case, _ in hits]
    matched = {case.name: terms for case, terms in hits}
    body = render(cases, f"## 历史案例（查询词: {query}）")
    trailer = "\n".join(f"- {name}: 命中 {', '.join(terms)}" for name, terms in matched.items())
    return f"{body}\n\n命中详情：\n{trailer}"
