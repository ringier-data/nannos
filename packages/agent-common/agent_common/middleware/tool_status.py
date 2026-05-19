"""Tool-call status middleware — emits descriptive status for all tool calls.

Intercepts every tool call via ``awrap_tool_call`` and emits a human-friendly
status message via ``stream_writer`` **before** the tool executes.  Messages
are path-aware for filesystem tools:

* ``read_file /skills/{name}/…`` → ``"Loading skill {name}…"``
* ``read_file /project/main.py`` → ``"Reading /project/main.py…"``
* ``grep …``                     → ``"Using grep…"``

The custom event is picked up by the streaming loop in ``dynamic_agent.py``
and forwarded as an activity-log ``TaskUpdate``.  This replaces the
``tool_call_chunks`` detection in the streaming loop, which only had access
to incomplete/partial args.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.types import Command
from langgraph.typing import ContextT

logger = logging.getLogger(__name__)

# Custom-event key used for stream_writer emissions.
TOOL_STATUS_EVENT = "tool_status"

# Tools that should never emit status (internal schema tools).
_SUPPRESSED_TOOLS = frozenset({"FinalResponseSchema", "SubAgentResponseSchema"})


class ToolStatusMiddleware(AgentMiddleware[AgentState, ContextT]):
    """Emits descriptive status messages for all tool calls."""

    state_schema = AgentState

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")

        if tool_name and tool_name not in _SUPPRESSED_TOOLS:
            args = request.tool_call.get("args", {})
            status = _build_status(tool_name, args)
            if status:
                await _emit_status(status)

        return await handler(request)


def _build_status(tool_name: str, args: dict) -> str | None:
    """Return a human-readable status string, or *None* to skip."""
    if tool_name == "read_file":
        file_path = args.get("file_path") or args.get("path", "")
        if not file_path:
            return f"Using {tool_name}\u2026"

        # Skill path: /skills/{skill_name}/…
        if file_path.startswith("/skills/"):
            parts = PurePosixPath(file_path).parts  # ('/', 'skills', name, …)
            if len(parts) >= 3:
                return f"Loading skill {parts[2]}\u2026"
            return "Loading skill\u2026"

        return f"Reading {file_path}\u2026"

    if tool_name == "execute":
        command = args.get("command", "")
        if command:
            return f"Running `{_truncate(command, 80)}`\u2026"

    if tool_name in ("write_file", "edit_file"):
        file_path = args.get("file_path", "")
        if file_path:
            verb = "Writing" if tool_name == "write_file" else "Editing"
            return f"{verb} {file_path}\u2026"

    if tool_name == "grep":
        pattern = args.get("pattern", "")
        if pattern:
            return f'Searching for "{_truncate(pattern, 60)}"\u2026'

    if tool_name == "docstore_search":
        query = args.get("query", "")
        if query:
            return f'Searching documents for "{_truncate(query, 60)}"\u2026'

    # Generic fallback for all other tools
    return f"Using {tool_name}\u2026"


def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* chars, appending '…' if trimmed."""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\u2026"


async def _emit_status(message: str) -> None:
    """Push a ``(TOOL_STATUS_EVENT, {...})`` custom event into the stream."""
    try:
        stream_writer = get_stream_writer()
    except Exception:
        return

    try:
        result = stream_writer((TOOL_STATUS_EVENT, {"status": message}))
        if inspect.iscoroutine(result):
            await result
    except Exception as e:
        logger.debug("Failed to emit tool status: %s", e)
