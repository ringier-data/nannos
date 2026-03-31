"""MCP progress callback for progressToken injection.

Providing a progress callback causes the MCP SDK to include a progressToken
in every tool-call request's ``_meta``, which signals the MCP gateway (Gatana)
to keep the connection alive during long-running tool executions.

Actual timeout protection is handled by the existing transport and session
layers:

* **Transport-level** — httpx SSE ``sse_read_timeout`` is a per-event idle
  timeout that resets on every SSE event, including progress notifications.
* **JSON-RPC level** — ``ClientSession(read_timeout_seconds=...)`` provides a
  hard upper-bound via ``anyio.fail_after`` on the response wait.
"""

from __future__ import annotations

import logging

from langchain_mcp_adapters.callbacks import CallbackContext

logger = logging.getLogger(__name__)


async def on_mcp_progress(
    progress: float,
    total: float | None,
    message: str | None,
    context: CallbackContext,
) -> None:
    """Log MCP progress notifications at DEBUG level.

    This callback is intentionally lightweight — its primary purpose is to
    trigger the MCP SDK's automatic ``progressToken`` injection rather than
    to process the notifications themselves.
    """
    server = context.server_name or "unknown"
    tool = context.tool_name or "unknown"
    pct = f"{progress / total * 100:.0f}%" if total else f"{progress}"
    logger.debug(f"MCP progress [{server}/{tool}]: {pct} - {message or ''}")
