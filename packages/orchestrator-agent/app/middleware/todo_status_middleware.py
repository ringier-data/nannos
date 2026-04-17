"""Todo List Status Update Middleware for A2A protocol.

This middleware detects when the LLM calls write_todos (from TodoListMiddleware)
and emits a structured snapshot of the full todo list via stream_writer.

Each snapshot contains all todos with their current state, using the schema:
  [{ "name": str, "state": "submitted" | "working" | "completed" | "failed" }]

Snapshot semantics: the entire list is sent each time since items can be
added or modified between calls.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.types import Command
from langgraph.typing import ContextT
from ringier_a2a_sdk.models import TODO_STATE_MAP, TodoItem

logger = logging.getLogger(__name__)


class TodoStatusState(AgentState):
    """Extended agent state (no additional fields needed for streaming approach)."""

    pass


class TodoStatusMiddleware(AgentMiddleware[TodoStatusState, ContextT]):
    """Middleware that emits structured todo snapshots when the LLM updates the todo list.

    Each write_todos interception produces a single snapshot event containing the
    full list of todos with mapped states.
    """

    state_schema = TodoStatusState

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Intercept write_todos calls and emit a structured snapshot."""
        tool_name = request.tool_call.get("name", "")

        if tool_name != "write_todos":
            return await handler(request)

        logger.info("[TODO MIDDLEWARE] Intercepting write_todos call")

        # Obtain stream_writer via the official LangGraph API (contextvars-based)
        stream_writer = None
        try:
            stream_writer = get_stream_writer()
        except Exception:
            pass

        # Extract todos and emit snapshot before executing the tool
        args = request.tool_call.get("args", {})
        todos = args.get("todos", [])

        if todos and stream_writer:
            snapshot = self._build_snapshot(todos)
            await self._emit_snapshot(stream_writer, snapshot)

        # Execute the tool normally
        return await handler(request)

    @staticmethod
    def _build_snapshot(todos: list[dict]) -> list[TodoItem]:
        """Build a structured snapshot from the raw todo list."""
        return [
            TodoItem(
                name=todo.get("content", "Task"),
                state=TODO_STATE_MAP.get(todo.get("status", "pending"), "submitted"),  # type: ignore[arg-type]
            )
            for todo in todos
        ]

    @staticmethod
    async def _emit_snapshot(stream_writer: Callable, snapshot: list[TodoItem]) -> None:
        """Emit a todo snapshot event via stream_writer."""
        try:
            result = stream_writer(("todo_status", {"todos": snapshot}))
            if inspect.iscoroutine(result):
                await result  # type: ignore
            logger.debug("[TODO MIDDLEWARE] Emitted todo snapshot (%d items)", len(snapshot))
        except Exception as e:
            logger.warning(f"[TODO MIDDLEWARE] Failed to emit snapshot: {e}")
