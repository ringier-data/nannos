"""Sandbox path hint middleware — enriches execute() errors with virtual FS guidance.

When a sandbox-enabled agent runs execute() commands that reference virtual
filesystem paths (/memories/, /skills/, etc.), the commands fail because those
paths don't exist in the sandbox container. This middleware:

1. **Pre-execution**: Scans the command for virtual route prefixes and flags them.
2. **Post-execution (failure)**: Scans error output for virtual paths and appends
   a remediation hint pointing to ``copy_to_sandbox()``.
3. **Post-execution (success + flagged)**: Appends a warning that virtual paths
   were detected and may cause issues in future runs.

Only added to the middleware stack for sandbox-enabled agents.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT

logger = logging.getLogger(__name__)

# Virtual filesystem route prefixes that don't exist in the sandbox.
_VIRTUAL_ROUTES = (
    "/memories/",
    "/skills/",
    "/channel_memories/",
    "/group_memories/",
    "/large_tool_results/",
)

# Pattern to find virtual route references in text.
# Matches route prefix + path characters (letters, digits, dots, hyphens,
# underscores, slashes).  Excludes trailing punctuation like colons from
# shell error messages (e.g. "cat: /memories/foo.txt: No such file").
_VIRTUAL_PATH_PATTERN = re.compile(r"(?<!\w)(" + "|".join(re.escape(r) for r in _VIRTUAL_ROUTES) + r")[\w./\-]*")

# Error indicators in execute() output.
_ERROR_INDICATORS = (
    "No such file or directory",
    "FileNotFoundError",
    "Permission denied",
    "not found",
    "cannot open",
    "does not exist",
)


class SandboxPathHintMiddleware(AgentMiddleware[AgentState, ContextT]):
    """Enriches execute() tool results with virtual filesystem path hints.

    Detects when execute() commands reference virtual filesystem paths
    and appends actionable guidance about using copy_to_sandbox().
    """

    state_schema = AgentState

    def __init__(self, sandbox_home: str) -> None:
        self._sandbox_home = sandbox_home

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")

        # Only intercept execute() calls
        if tool_name != "execute":
            return await handler(request)

        # Pre-execution: scan command for virtual route prefixes
        command = request.tool_call.get("args", {}).get("command", "")
        flagged_paths = _find_virtual_paths(command)

        # Execute the command
        result = await handler(request)

        # Only enrich ToolMessage results (not Command)
        if not isinstance(result, ToolMessage):
            return result

        content = result.content if isinstance(result.content, str) else str(result.content)

        # Post-execution: check for errors referencing virtual paths
        output_paths = _find_virtual_paths(content)
        has_error = any(indicator in content for indicator in _ERROR_INDICATORS)

        hint = None
        if has_error and output_paths:
            # Error output contains virtual paths — append remediation hint
            paths_list = ", ".join(f"'{p}'" for p in output_paths)
            hint = (
                f"\n\n⚠️ The error above references virtual filesystem path(s): {paths_list}. "
                f"These paths are on the virtual filesystem and are NOT directly accessible "
                f"in the sandbox. To use them in execute() commands:\n"
                f"  1. Call copy_to_sandbox('{output_paths[0]}') to materialize the file\n"
                f"  2. Use the returned sandbox path (under {self._sandbox_home}/) in your command"
            )
        elif flagged_paths and not has_error:
            # Command had virtual paths but succeeded — warn about fragility
            paths_list = ", ".join(f"'{p}'" for p in flagged_paths)
            hint = (
                f"\n\n⚠️ This command references virtual filesystem path(s): {paths_list}. "
                f"If this worked, the file may have been pre-synced (e.g., skills at "
                f"{self._sandbox_home}/skills/). For other virtual paths, prefer using "
                f"copy_to_sandbox() to ensure reliability across runs."
            )
        elif flagged_paths and has_error:
            # Command had virtual paths and failed — strong remediation hint
            paths_list = ", ".join(f"'{p}'" for p in flagged_paths)
            hint = (
                f"\n\n⚠️ The command references virtual filesystem path(s): {paths_list}. "
                f"These paths are NOT directly accessible in the sandbox. Use "
                f"copy_to_sandbox('{flagged_paths[0]}') to materialize the file first, "
                f"then use the returned sandbox path in your command."
            )

        if hint:
            return ToolMessage(
                content=content + hint,
                tool_call_id=result.tool_call_id,
                name=result.name,
            )

        return result


def _find_virtual_paths(text: str) -> list[str]:
    """Find virtual filesystem path references in text.

    Returns deduplicated list of matched paths.
    """
    matches = _VIRTUAL_PATH_PATTERN.findall(text)
    # findall returns the group (just the route prefix) when there's a group
    # We want the full match, so use finditer instead
    full_matches = [m.group(0) for m in _VIRTUAL_PATH_PATTERN.finditer(text)]
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for path in full_matches:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result
