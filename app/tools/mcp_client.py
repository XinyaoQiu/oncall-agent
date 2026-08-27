"""MCP connection management.

Ported from the sibling project, with three defects fixed:

- The server map there is bound at *import* time (`DEFAULT_MCP_SERVERS = config.mcp_servers`),
  so importing the agent package freezes whatever configuration happened to be loaded first
  and tests cannot vary it. Here the map is read from `Settings` when a holder is built.
- Its `get_mcp_client()` is a module-global singleton that silently ignores the `servers` and
  `tool_interceptors` arguments on every call after the first — a caller asking for a
  different configuration gets the old one back with no warning. `McpClientHolder` owns one
  client explicitly, and a second configuration means a second holder.
- Nothing ever closed the client. `aclose()` exists so a Slack process can drop its
  connections on shutdown.

What is kept: the retry interceptor, the `load_tools_safe()` degrade-to-empty pattern, and
the ExceptionGroup flattening — MCP failures arrive as TaskGroup groups whose real cause is
three levels down, and without flattening the log says only "unhandled errors in a TaskGroup".
"""

import asyncio
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from loguru import logger
from mcp.types import CallToolResult, TextContent

from app.config import Settings

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0


def format_exception_chain(exc: BaseException) -> str:
    """Flatten ExceptionGroup / TaskGroup nesting so a log line names the real cause."""
    sub_exceptions = getattr(exc, "exceptions", None)
    if sub_exceptions is not None:
        lines = [str(exc)]
        for i, sub in enumerate(sub_exceptions):
            lines.append(f"  [{i}] {format_exception_chain(sub)}")
        return "\n".join(lines)
    msg = f"{type(exc).__name__}: {exc}"
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return f"{msg}\n  caused by: {format_exception_chain(cause)}"
    return msg


def suggest_mcp_transport(url: str, transport: str) -> str | None:
    """Flag an obvious URL/transport mismatch. Never rewrites the configuration."""
    lower = url.lower()
    normalized = transport.replace("_", "-")
    if "/sse" in lower and normalized in ("streamable-http", "http"):
        return f"MCP url {url} looks like an SSE endpoint but transport={transport!r}"
    if normalized == "sse" and "/mcp" in lower and "/sse" not in lower:
        return f"MCP url {url} looks like a streamable-http endpoint but transport={transport!r}"
    return None


async def retry_interceptor(
    request: MCPToolCallRequest,
    handler: Any,
    max_retries: int = MAX_RETRIES,
    delay: float = RETRY_BASE_DELAY,
) -> Any:
    """Retry a failing MCP tool call with exponential backoff.

    Exhausting the retries returns an error `CallToolResult` rather than raising: one flaky
    server must not abort a whole investigation, and the error text reaches the model as an
    observation it can plan around.
    """
    last_error: BaseException | None = None
    for attempt in range(max_retries):
        try:
            result = await handler(request)
            if attempt:
                logger.info(f"mcp tool {request.name} succeeded on attempt {attempt + 1}")
            return result
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"mcp tool {request.name} failed "
                f"({attempt + 1}/{max_retries}): {format_exception_chain(exc)}"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(delay * (2**attempt))

    error_msg = f"mcp tool {request.name} failed after {max_retries} attempts: {last_error}"
    logger.error(error_msg)
    return CallToolResult(content=[TextContent(type="text", text=error_msg)], isError=True)


class McpClientHolder:
    """Owns one `MultiServerMCPClient` for one server map.

    Construction does no I/O; the client is built on first use. Two different configurations
    mean two holders, which is the whole reason this is not a module-level singleton.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        servers: dict[str, dict[str, Any]] | None = None,
        tool_interceptors: list[Any] | None = None,
        with_retry: bool = True,
    ) -> None:
        self.servers: dict[str, dict[str, Any]] = dict(
            servers if servers is not None else settings.mcp_servers
        )
        self.interceptors: list[Any] = list(tool_interceptors or [])
        if with_retry:
            self.interceptors.insert(0, retry_interceptor)
        self._client: MultiServerMCPClient | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.servers)

    def transport_warnings(self) -> list[str]:
        out = []
        for name, conn in self.servers.items():
            hint = suggest_mcp_transport(conn.get("url", ""), conn.get("transport", ""))
            if hint:
                out.append(f"{name}: {hint}")
        return out

    async def client(self) -> MultiServerMCPClient:
        async with self._lock:
            if self._client is None:
                logger.info(f"building MCP client for servers: {sorted(self.servers)}")
                kwargs: dict[str, Any] = {}
                if self.interceptors:
                    kwargs["tool_interceptors"] = self.interceptors
                self._client = MultiServerMCPClient(self.servers, **kwargs)  # type: ignore[arg-type]
            return self._client

    async def get_tools(self) -> list[BaseTool]:
        """Tools from every configured server. Raises; use `load_tools_safe()` on a hot path."""
        if not self.servers:
            return []
        client = await self.client()
        return list(await client.get_tools())

    async def load_tools_safe(self) -> tuple[list[BaseTool], str | None]:
        """Never raises: an unreachable MCP server degrades to no tools plus a readable error."""
        if not self.servers:
            return [], None
        try:
            return await self.get_tools(), None
        except BaseException as exc:
            detail = format_exception_chain(exc)
            logger.error(f"MCP tool load failed: {detail}")
            return [], detail

    async def aclose(self) -> None:
        """Drop the client. Sessions in langchain-mcp-adapters are per-call, so this only
        releases the client itself; it exists so shutdown is explicit rather than GC-timed."""
        async with self._lock:
            self._client = None
