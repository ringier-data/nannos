"""MCP progress callback for keeping Gatana connections alive.

Providing a progress callback causes the MCP SDK to include a progressToken
in every tool-call request's ``_meta``, which signals the MCP gateway (Gatana)
to keep the connection alive during long-running tool executions.
"""

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
