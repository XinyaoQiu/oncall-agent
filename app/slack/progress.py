"""The progress surface: one message, edited under a rate limit.

A channel watching a silent bot for ninety seconds assumes it has hung, so progress has to
be visible. But `chat.update` is Tier 3 and Slack asks for roughly one request per second
per channel, and the old adapter called it once per investigation step with no batching — a
fast run burst-edits its way into 429s and then loses the updates it dropped.

So updates are coalesced: the first lands immediately (the engineer needs to see it
started), then at most one edit per `interval`, and the final one always lands. A 429 does
not drop an update — it lengthens the interval and reschedules, because the update that gets
dropped is the last one, which is the answer.

`chat.startStream`/`appendStream` (Tier 4) is the better path and is behind
`slack_use_streaming`, default off: the API reference says `thread_ts` returns
`invalid_thread_ts` outside whole-channel sessions while the SDK's own examples pass it
(spec §12 risk 1). The adapter tries it and falls back to `chat.update` on that error, so
turning the flag on in a real workspace cannot leave a turn without a reply.
"""

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from loguru import logger

MAX_CHARS = 3800
MAX_LINES = 8
HEADER = ":mag: Investigating…"


def _payload(exc: Exception) -> Any:
    response = getattr(exc, "response", None)
    return response if response is not None else {}


def _slack_error(exc: Exception) -> str:
    try:
        return _payload(exc).get("error") or ""
    except Exception:
        return ""


def _retry_after(exc: Exception) -> float | None:
    """Seconds Slack asked us to wait, from the 429 header."""
    payload = _payload(exc)
    try:
        status = getattr(payload, "status_code", None) or payload.get("status_code")
        headers = getattr(payload, "headers", None) or payload.get("headers") or {}
        value = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if value is None and status != 429:
        return None
    try:
        return float(value) if value is not None else 60.0
    except (TypeError, ValueError):
        return 60.0


def _value(response: Any, key: str) -> Any:
    if response is None:
        return None
    try:
        return response.get(key)
    except Exception:
        return None


def _chunks(text: str, size: int = MAX_CHARS) -> list[str]:
    if len(text) <= size:
        return [text]
    out, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > size and current:
            out.append(current)
            current = ""
        while len(line) > size:
            out.append(line[:size])
            line = line[size:]
        current += line
    if current:
        out.append(current)
    return out


class ProgressWriter:
    """Coalescing writer over one thread message."""

    def __init__(
        self,
        client: Any,
        channel: str,
        thread_ts: str,
        *,
        interval: float = 1.5,
        use_streaming: bool = False,
        header: str = HEADER,
    ) -> None:
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.interval = interval
        self.use_streaming = use_streaming
        self.header = header
        self.ts: str | None = None
        self._lines: list[str] = []
        self._last_write = 0.0
        self._timer: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._delivered = False
        self._stream_ts: str | None = None
        self._streamed = ""

    async def update(self, lines: Sequence[str]) -> None:
        """Show `lines`. Emits now if the interval has elapsed, otherwise schedules."""
        if self._closed:
            return
        self._lines = [line for line in lines if line]
        if self.ts is None and self._stream_ts is None:
            await self._flush()
            return
        waited = time.monotonic() - self._last_write
        if waited >= self.interval:
            await self._flush()
        else:
            self._schedule(self.interval - waited)

    async def finish(self, text: str) -> None:
        """The final content. Always emitted, whatever the interval or a 429 says."""
        self._cancel_timer()
        self._closed = True
        for attempt in range(3):
            await self._flush(final=text)
            if self._delivered:
                return
            await asyncio.sleep(min(self.interval, 5.0) if attempt else 0)
        logger.error("progress: the final message could not be delivered to slack")

    def _schedule(self, delay: float) -> None:
        if self._timer is not None and not self._timer.done():
            return
        self._timer = asyncio.create_task(self._after(delay))

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def _after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._closed:
            await self._flush()

    def _render(self) -> str:
        lines = self._lines[-MAX_LINES:]
        return "\n".join([self.header, *(f"• {line}" for line in lines)]) if lines else self.header

    async def _flush(self, final: str | None = None) -> None:
        async with self._lock:
            text = final if final is not None else self._render()
            self._delivered = False
            if not text.strip():
                return
            if self.use_streaming and await self._stream(text, final=final is not None):
                self._delivered = True
                self._last_write = time.monotonic()
                return
            parts = _chunks(text)
            if self.ts is None:
                response = await self._call("chat_postMessage", text=parts[0])
                self.ts = _value(response, "ts")
                if self.ts is None:
                    return
            elif await self._call("chat_update", ts=self.ts, text=parts[0]) is None:
                return
            for part in parts[1:]:
                await self._call("chat_postMessage", text=part)
            self._delivered = True
            self._last_write = time.monotonic()

    async def _stream(self, text: str, *, final: bool) -> bool:
        if self._stream_ts is None:
            response = await self._call("chat_startStream")
            self._stream_ts = _value(response, "ts")
            if self._stream_ts is None:
                return False
            self._streamed = ""
        delta = text[len(self._streamed) :] if text.startswith(self._streamed) else "\n" + text
        if delta:
            await self._call("chat_appendStream", ts=self._stream_ts, markdown_text=delta)
        self._streamed = text
        if final:
            await self._call("chat_stopStream", ts=self._stream_ts)
        return True

    async def _call(self, method: str, **kwargs: Any) -> Any:
        kwargs.setdefault("channel", self.channel)
        if method in ("chat_postMessage", "chat_startStream"):
            kwargs.setdefault("thread_ts", self.thread_ts)
        call = getattr(self.client, method, None)
        try:
            if call is None:
                return await self.client.api_call(method.replace("_", ".", 1), json=kwargs)
            return await call(**kwargs)
        except Exception as exc:
            return self._handle_failure(method, exc)

    def _handle_failure(self, method: str, exc: Exception) -> None:
        retry_after = _retry_after(exc)
        if retry_after is not None:
            self.interval = max(self.interval, retry_after)
            logger.warning(f"slack rate-limited on {method}; interval now {self.interval}s")
            if not self._closed:
                self._schedule(retry_after)
            return None
        if _slack_error(exc) == "invalid_thread_ts" and self.use_streaming:
            logger.warning("chat.startStream rejected thread_ts; falling back to chat.update")
            self.use_streaming = False
            self._stream_ts = None
            return None
        logger.warning(f"slack {method} failed: {exc}")
        return None
