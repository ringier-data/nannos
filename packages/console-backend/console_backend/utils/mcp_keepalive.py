"""Progress-notification keepalive for long-running MCP tool calls.

Prevents 504 Gateway Time-out errors from the ALB / reverse proxy in front of
console-backend's ``/mcp`` endpoint when a tool runs longer than the proxy's
idle timeout (e.g. ``console_web_search``, which makes a full grounded LLM call).

This is the MCP-transport analog of ``utils/streaming.py``'s whitespace
keepalive. We cannot reuse that approach here: fastapi_mcp invokes the tool's
FastAPI route through an internal ``httpx.ASGITransport`` client and reads the
whole body with ``response.json()``, so any bytes a route streams are consumed
by that internal client and never reach the outer ``/mcp`` SSE connection the
ALB sees. The connection-keeping signal that *does* reach the ALB is an MCP
**progress notification**: the SDK writes it as an SSE event on the per-request
stream, flushing bytes through the proxy and resetting its idle timer (and the
client's ``sse_read_timeout`` — see ``ringier_a2a_sdk.utils.mcp_progress``).

The orchestrator already injects a ``progressToken`` on every console tool call
(it registers an ``on_progress`` callback). We still always send a log-level
notification too: ``progress`` notifications are silently dropped by the SDK when
the client supplied no ``progressToken``, whereas ``notifications/message`` are
unconditional, so the log message is the keepalive that always reaches the wire.

Usage — wrap the coroutine that runs the tool, inside an MCP request context::

    result = await with_progress_keepalive(slow_tool_coro(), mcp.server)
"""

import asyncio
import logging
import os
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Seconds between keepalive progress notifications. Must be comfortably below the
# proxy idle timeout (AWS ALB default 60s) so a notification always lands before
# the connection is considered idle. Override via env for ops tuning.
_KEEPALIVE_INTERVAL: float = float(os.environ.get("MCP_KEEPALIVE_INTERVAL_SECONDS", "15"))


async def with_progress_keepalive(
    coro: Coroutine[Any, Any, Any],
    server: Any,
    *,
    interval: float = _KEEPALIVE_INTERVAL,
) -> Any:
    """Run *coro*, emitting periodic MCP progress notifications while it executes.

    1. The coroutine is started immediately.
    2. While it is running, a keepalive is sent every *interval* seconds to keep
       the ``/mcp`` SSE connection alive through the ALB — always a log-level
       notification, plus a progress notification when the client gave a token.
    3. The coroutine's result is returned (or its exception re-raised) unchanged,
       so fastapi_mcp's normal result/error handling is preserved.

    If no MCP request context is active (no SSE stream to write to), the coroutine
    is awaited with no keepalive.
    """
    task = asyncio.ensure_future(coro)

    progress_token = None
    session = None
    request_id = None
    try:
        ctx = server.request_context
        progress_token = ctx.meta.progressToken if ctx.meta else None
        session = ctx.session
        request_id = ctx.request_id
    except LookupError:
        # No active MCP request context — nothing to keep alive against.
        pass
    except AttributeError:
        logger.debug("MCP keepalive: unexpected request context shape", exc_info=True)

    if session is None:
        return await task

    total = 1.0
    steps = 0
    while True:
        try:
            # asyncio.shield so a per-interval timeout cancels only the wait, not
            # the underlying tool call. Returns/raises exactly what the tool does.
            return await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            steps += 1
            # Asymptotic curve: approaches `total` but never reaches it.
            current = total * (1 - 1 / (1 + steps * 0.15))
            # Progress notification (skipped by the SDK if no progressToken).
            if progress_token is not None:
                try:
                    await session.send_progress_notification(
                        progress_token=progress_token,
                        progress=current,
                        total=total,
                        message="Tool still running…",
                        related_request_id=request_id,
                    )
                except Exception:
                    logger.debug("MCP keepalive: progress notification failed", exc_info=True)
            # Log notification — token-independent, so this is the keepalive that
            # always reaches the wire. A failed keepalive must never break the tool
            # call; if the SSE stream is gone the awaited task surfaces the real error.
            try:
                await session.send_log_message(
                    level="debug",
                    data=f"Processing… {current / total:.0%}",
                    related_request_id=request_id,
                )
            except Exception:
                logger.debug("MCP keepalive: log notification failed", exc_info=True)
