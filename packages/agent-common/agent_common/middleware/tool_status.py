"""Tool-call status middleware — emits descriptive status for all tool calls.

Intercepts every tool call via ``awrap_tool_call`` and emits a human-friendly
status message via ``stream_writer`` **before** the tool executes.  Messages
are path-aware for filesystem tools:

* ``load_skill name={name}``                → ``"Loading skill {name}…"``
* ``read_file /skills/{name}/…``            → ``"Loading skill {name}…"``
* ``read_file /skills/{name}/… offset/limit`` → ``"Reading skill {name} (lines A–B)…"``
* ``grep pattern path=/skills/{name}``      → ``'Searching skill {name} for "pattern"…'``
* ``ls|glob path=/skills/{name}``           → ``"Looking for files in skill {name}…"``
* ``read_file /project/main.py``            → ``"Reading /project/main.py…"``
* ``grep …``                                → ``'Searching for "…"…'``

The same labels apply when the call is routed through the PTC ``eval`` tool
as ``tools.readFile({…})`` / ``tools.grep({…})`` / ``tools.ls({…})``.

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

from agent_common.core.notify_user_tool import NOTIFY_USER_TOOL_NAME
from agent_common.core.load_skill_tool import LOAD_SKILL_TOOL_NAME
from agent_common.middleware.ptc_guard import PTC_CODE_INTERPRETER_TOOL_NAME

logger = logging.getLogger(__name__)

# Custom-event key used for stream_writer emissions.
TOOL_STATUS_EVENT = "tool_status"

# Tools that should never emit status (internal schema tools), plus ``notify_user``:
# its whole output IS an activity line (the note itself), so a "Using notify_user…"
# label lands directly above the note and says nothing the note does not.
_SUPPRESSED_TOOLS = frozenset({"FinalResponseSchema", "SubAgentResponseSchema", NOTIFY_USER_TOOL_NAME})

# Matches ``tools.<camelCaseName>(`` calls inside a PTC ``eval`` snippet — the
# dot-notation form the PTC prompt instructs the model to use.
_PTC_TOOL_CALL_RE = re.compile(r"\btools\.([A-Za-z_$][\w$]*)\s*\(")
# Matches ``tools.<name>({ … })`` calls whose argument is a single, non-nested
# object literal, capturing the name and the object body. Used to describe a
# skill read/search routed through the code interpreter the way the native
# tool call would be.
_PTC_CALL_WITH_ARGS_RE = re.compile(r"\btools\.([A-Za-z_$][\w$]*)\s*\(\s*\{([^{}]*)\}")
# One ``key: value`` pair inside that object body; value is a quoted string or a
# bare integer. Other value types are ignored (they never carry a path/range).
_PTC_ARG_RE = re.compile(r"\b(\w+)\s*:\s*(?:(['\"`])(.*?)\2|(-?\d+))")
# PTC tools whose ``/skills/`` calls get a skill-specific label.
_PTC_SKILL_TOOLS = frozenset({"read_file", "grep", "ls", "glob"})
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
                await _emit_status(status, tool_name)

        return await handler(request)


def _skill_name(path: str) -> str | None:
    """Return the skill name of a ``/skills/{name}/…`` path, or *None*."""
    if not path.startswith("/skills/"):
        return None
    parts = PurePosixPath(path).parts  # ('/', 'skills', name, …)
    return parts[2] if len(parts) >= 3 else None


def _describe_skill_path(file_path: str) -> str:
    """Describe a read of a ``/skills/{name}/…`` path as a skill load."""
    name = _skill_name(file_path)
    if name:
        return f"Loading skill {name}\u2026"
    return "Loading skill\u2026"


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _describe_skill_read(file_path: str, args: dict) -> str:
    """Describe a ``read_file`` on a skill path.

    A full read is a skill *load*. A windowed read (``offset``/``limit``) is the
    model paging through a large skill file after a search, so it is labelled as
    a read of a line range instead of a repeated load.
    """
    name = _skill_name(file_path)
    offset = _as_int(args.get("offset"))
    limit = _as_int(args.get("limit"))
    if name is None or (not offset and limit is None):
        return _describe_skill_path(file_path)
    start = (offset or 0) + 1  # offset is 0-based; the tool prints 1-based line numbers
    if limit is None:
        return f"Reading skill {name} (from line {start})\u2026"
    return f"Reading skill {name} (lines {start}\u2013{start + limit - 1})\u2026"


def _describe_skill_search(tool_name: str, name: str, args: dict) -> str:
    """Describe a ``grep``/``ls``/``glob`` scoped to a skill folder."""
    if tool_name == "grep":
        pattern = args.get("pattern", "")
        if pattern:
            return f'Searching skill {name} for "{_truncate(pattern, 60)}"\u2026'
        return f"Searching skill {name}\u2026"
    return f"Looking for files in skill {name}\u2026"


def _build_status(tool_name: str, args: dict) -> str | None:
    """Return a human-readable status string, or *None* to skip."""
    if tool_name == LOAD_SKILL_TOOL_NAME:
        name = args.get("name", "")
        return f"Loading skill {name}\u2026" if name else "Loading skill\u2026"

    if tool_name == "read_file":
        file_path = args.get("file_path") or args.get("path", "")
        if not file_path:
            return f"Using {tool_name}\u2026"

        # Skill path: /skills/{skill_name}/…
        if file_path.startswith("/skills/"):
            return _describe_skill_read(file_path, args)

        return f"Reading {file_path}\u2026"

    if tool_name in ("grep", "ls", "glob"):
        skill = _skill_name(args.get("path") or "")
        if skill:
            return _describe_skill_search(tool_name, skill, args)

    if tool_name == PTC_CODE_INTERPRETER_TOOL_NAME:
        # The code interpreter is an opaque REPL; surface what it will actually
        # do. Prefer listing the tools the snippet calls (``tools.<name>(\u2026)``);
        # fall back to the code itself when it is pure computation.
        code = args.get("code", "")
        if not code:
            return f"Using {tool_name}\u2026"
        # A skill load or search reaches the embedded agent as a
        # ``tools.readFile``/``tools.grep``/``tools.ls`` of a /skills/ path inside
        # an eval snippet; describe it the same way the native tool call would,
        # not as the generic "Running read_file\u2026".
        for name, call_args in _extract_ptc_calls_with_args(code):
            if name not in _PTC_SKILL_TOOLS:
                continue
            path = call_args.get("file_path") or call_args.get("path") or ""
            if path.startswith("/skills/"):
                return _build_status(name, call_args)
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


def _extract_ptc_calls_with_args(code: str) -> list[tuple[str, dict]]:
    """Return ``(snake_case_name, args)`` for each ``tools.<name>({…})`` call, in order.

    Only string and integer values are captured, and camelCase arg keys are
    normalised to snake_case (``filePath`` → ``file_path``) so the result can be
    fed to :func:`_build_status` as if it were a native tool call.
    """
    calls: list[tuple[str, dict]] = []
    for match in _PTC_CALL_WITH_ARGS_RE.finditer(code):
        args: dict = {}
        for arg in _PTC_ARG_RE.finditer(match.group(2)):
            key = _camel_to_snake(arg.group(1))
            args[key] = int(arg.group(4)) if arg.group(4) is not None else arg.group(3)
        calls.append((_camel_to_snake(match.group(1)), args))
    return calls


def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* chars, appending '…' if trimmed."""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\u2026"


async def _emit_status(message: str, tool_name: str) -> None:
    """Push a ``(TOOL_STATUS_EVENT, {...})`` custom event into the stream.

    The payload carries the originating ``tool`` name alongside the human
    ``status`` so consumers can route by tool — e.g. the orchestrator surfaces
    only ``eval`` from this channel (its other tools come from the ``messages``
    stream), while sub-agents forward every status generically.
    """
    try:
        stream_writer = get_stream_writer()
    except Exception:
        return

    try:
        result = stream_writer((TOOL_STATUS_EVENT, {"status": message, "tool": tool_name}))
        if inspect.iscoroutine(result):
            await result
    except Exception as e:
        logger.debug("Failed to emit tool status: %s", e)
