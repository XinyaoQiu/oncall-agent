"""Cross-cutting plumbing: the model factory, and the shape tool loading arrives in.

Nothing here imports `app.tools` or `app.graph`; it is depended *on*, never depending.
"""

from collections.abc import Awaitable, Callable

from langchain_core.tools import BaseTool

from app.core.llm_factory import LLMUnavailable, get_llm, model_name

# Tools are loaded once per turn, asynchronously, and the warnings ride along: an
# unreachable MCP server is a shorter toolset and a disclosure, never an exception.
ToolLoader = Callable[[], Awaitable[tuple[list[BaseTool], list[str]]]]

__all__ = ["LLMUnavailable", "ToolLoader", "get_llm", "model_name"]
