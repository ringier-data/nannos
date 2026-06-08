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
import re
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.types import Command
from langgraph.typing import ContextT

from agent_common.middleware.ptc_guard import PTC_CODE_INTERPRETER_TOOL_NAME

logger = logging.getLogger(__name__)

# Custom-event key used for stream_writer emissions.
TOOL_STATUS_EVENT = "tool_status"

# Tools that should never emit status (internal schema tools).
_SUPPRESSED_TOOLS = frozenset({"FinalResponseSchema", "SubAgentResponseSchema"})

# Matches ``tools.<camelCaseName>(`` calls inside a PTC ``eval`` snippet — the
# dot-notation form the PTC prompt instructs the model to use.
_PTC_TOOL_CALL_RE = re.compile(r"\btools\.([A-Za-z_$][\w$]*)\s*\(")
# camelCase word boundary, used to invert the PTC ``snake_case → camelCase`` map.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Max distinct tool names to list in an ``eval`` status before summarising.
_PTC_STATUS_MAX_TOOLS = 5


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

    if tool_name == PTC_CODE_INTERPRETER_TOOL_NAME:
        # The code interpreter is an opaque REPL; surface what it will actually
        # do. Prefer listing the tools the snippet calls (``tools.<name>(\u2026)``);
        # fall back to the code itself when it is pure computation.
        code = args.get("code", "")
        if not code:
            return f"Using {tool_name}\u2026"
        called = _extract_ptc_tool_calls(code)
        if called:
            shown = ", ".join(called[:_PTC_STATUS_MAX_TOOLS])
            extra = len(called) - _PTC_STATUS_MAX_TOOLS
            if extra > 0:
                shown += f" +{extra} more"
            return f"Running {shown}\u2026"
        return f"Running `{_truncate(code, 80)}`\u2026"

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


def _camel_to_snake(name: str) -> str:
    """Best-effort inverse of the PTC ``snake_case → camelCase`` tool-name map.

    ``fetchFetchMarkdown`` → ``fetch_fetch_markdown``. Names that were already
    snake_case (PTC leaves names whose separators are not followed by a lowercase
    letter unchanged, e.g. ``tool_2``) round-trip unchanged.
    """
    return _CAMEL_BOUNDARY_RE.sub("_", name).lower()


def _extract_ptc_tool_calls(code: str) -> list[str]:
    """Return the distinct tools a PTC ``eval`` snippet calls, in first-seen order.

    The PTC bridge exposes tools as ``tools.<camelCaseName>(input)``; this scans
    for those calls and maps the names back to their canonical ``snake_case``
    form so the status reads in the same vocabulary the user knows the tools by.
    """
    seen: dict[str, None] = {}
    for match in _PTC_TOOL_CALL_RE.finditer(code):
        seen.setdefault(_camel_to_snake(match.group(1)), None)
    return list(seen)


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
