"""Todo List Status Update Middleware for A2A protocol.

This middleware detects when the LLM calls write_todos (from TodoListMiddleware)
and emits progressive status updates via stream_writer in real-time.

IMPLEMENTATION APPROACH:
- Uses awrap_tool_call hook to intercept write_todos tool calls
- Emits status messages via runtime.stream_writer as custom events
- Orchestrator receives events immediately in stream_mode='custom'
- No state updates needed - events delivered directly to client

This follows the same pattern as A2ATaskTrackingMiddleware for consistency.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT

logger = logging.getLogger(__name__)


class TodoStatusState(AgentState):
    """Extended agent state (no additional fields needed for streaming approach)."""

    pass


class TodoStatusMiddleware(AgentMiddleware[TodoStatusState, ContextT]):
    """Middleware to emit progressive status updates when LLM updates todo list.

    This middleware:
    - Intercepts write_todos tool calls in awrap_tool_call hook
    - Emits status updates based on current todo states
    - Reports in-progress and completed tasks
    - Orchestrator receives events in real-time with stream_mode='custom'

    This follows the same progressive streaming pattern as A2ATaskTrackingMiddleware.
    """

    state_schema = TodoStatusState

    def __init__(self):
        """Initialize the todo status middleware."""
        super().__init__()
        # Track if we've seen the initial plan per thread (simple flag)
        self._seen_initial: set[str] = set()

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Intercept write_todos calls and emit progressive status updates.

        This hook:
        1. Detects write_todos tool calls
        2. Emits initial plan on first call
        3. Emits status for in-progress tasks
        4. Executes tool normally via handler
        5. Emits status for completed tasks after execution

        Progressive events are delivered immediately to orchestrator using
        stream_mode='custom', following the A2A middleware pattern.
        """
        tool_name = request.tool_call.get("name", "")

        # Only intercept write_todos tool
        if tool_name != "write_todos":
            return await handler(request)

        logger.info("[TODO MIDDLEWARE] Intercepting write_todos call")

        # Extract stream_writer for progressive event emission
        stream_writer = None
        if request and hasattr(request, "runtime"):
            runtime = request.runtime
            if hasattr(runtime, "stream_writer") and runtime.stream_writer is not None:
                stream_writer = runtime.stream_writer
                logger.debug("[TODO MIDDLEWARE] Stream writer available")
            else:
                logger.warning("[TODO MIDDLEWARE] No stream_writer available")

        # Extract todos from tool call arguments
        args = request.tool_call.get("args", {})
        todos = args.get("todos", [])

        # Get thread_id for tracking from runtime config
        thread_id = "default"
        if hasattr(request, "runtime") and request.runtime:
            runtime = request.runtime
            if hasattr(runtime, "config") and runtime.config:
                config = runtime.config
                if isinstance(config, dict) and "configurable" in config:
                    thread_id = config["configurable"].get("thread_id", "default")
                    logger.info(f"[TODO MIDDLEWARE] Got thread_id from runtime.config: {thread_id}")

        logger.info(f"[TODO MIDDLEWARE] Using thread ID: {thread_id}")

        # Check if this is the first time we're seeing todos for this thread
        is_initial = thread_id not in self._seen_initial

        if todos and stream_writer:
            # Emit initial plan on first call
            if is_initial:
                status_message = self._format_initial_plan(todos)
                logger.info("[TODO MIDDLEWARE] Emitting initial plan")
                await self._emit_event(stream_writer, status_message, len(todos), "initial")
                self._seen_initial.add(thread_id)

            # Emit status for any in-progress tasks
            for todo in todos:
                if todo.get("status") == "in_progress":
                    status_message = self._format_single_todo_update(todo)
                    logger.info(f"[TODO MIDDLEWARE] Emitting in-progress status: {status_message}")
                    await self._emit_event(stream_writer, status_message, len(todos), "in_progress")

        # Execute the tool normally
        result = await handler(request)

        # Emit status for any completed or failed tasks after execution
        if todos and stream_writer:
            for todo in todos:
                if todo.get("status") == "completed":
                    completion_message = self._format_completion(todo)
                    logger.info(f"[TODO MIDDLEWARE] Emitting completion: {completion_message}")
                    await self._emit_event(stream_writer, completion_message, len(todos), "completion")
                elif todo.get("status") == "failed":
                    failure_message = self._format_failure(todo)
                    logger.info(f"[TODO MIDDLEWARE] Emitting failure: {failure_message}")
                    await self._emit_event(stream_writer, failure_message, len(todos), "failure")

        return result

    async def _emit_event(self, stream_writer: Callable, message: str, todo_count: int, event_subtype: str) -> None:
        """Emit a custom event via stream_writer.

        Args:
            stream_writer: The stream writer function
            message: The status message to emit
            todo_count: Total number of todos
            event_subtype: Type of event (initial, in_progress, completion, etc.)
        """
        try:
            result = stream_writer(
                (
                    "todo_status",
                    {
                        "message": message,
                        "todo_count": todo_count,
                        "subtype": event_subtype,
                        "timestamp": None,
                    },
                )
            )
            # Handle both sync and async stream_writer
            if inspect.iscoroutine(result):
                await result  # type: ignore
            logger.debug(f"[TODO MIDDLEWARE] Successfully emitted {event_subtype} event")
        except Exception as e:
            logger.warning(f"[TODO MIDDLEWARE] Failed to emit event: {e}")

    def _format_initial_plan(self, todos: list[dict]) -> str:
        """Format initial planning message with full plan.

        Args:
            todos: List of all todos in the plan

        Returns:
            Formatted message showing the complete plan
        """
        if not todos:
            return "📋 Task plan created."

        total = len(todos)

        # Show all tasks for initial plan (or first 10) in a compact format
        task_lines = []
        for i, task in enumerate(todos[:10], 1):
            content = task.get("content", f"Task {i}")

            # Truncate long content
            if len(content) > 80:
                content = content[:77] + "..."

            status = task.get("status", "pending")

            # Pick icon based on status
            if status == "completed":
                status_icon = "✅"
            elif status == "in_progress":
                status_icon = "🔄"
            elif status == "failed":
                status_icon = "❌"
            else:  # pending
                status_icon = "📌"

            task_lines.append(f"  {status_icon} {content}")

        if len(todos) > 10:
            remaining = len(todos) - 10
            task_lines.append(f"  ... and {remaining} more task{'s' if remaining != 1 else ''}")

        tasks_text = "\n".join(task_lines)
        return f"📋 **Plan created** ({total} task{'s' if total != 1 else ''}):\n{tasks_text}"

    def _format_single_todo_update(self, todo: dict) -> str:
        """Format a single todo status update.

        Args:
            todo: The todo that was updated

        Returns:
            Formatted message for single todo update
        """
        content = todo.get("content", "Task")

        # Truncate if too long
        if len(content) > 100:
            content = content[:97] + "..."

        status = todo.get("status", "pending")

        # Pick icon and message based on status
        if status == "in_progress":
            return f"🔄 **Working on**: {content}"
        elif status == "pending":
            return f"📌 **Queued**: {content}"
        else:
            # This shouldn't happen (completed is handled separately)
            return f"📋 {content}"

    def _format_completion(self, todo: dict) -> str:
        """Format a completion message for a todo.

        Args:
            todo: The completed todo

        Returns:
            Formatted completion message
        """
        content = todo.get("content", "Task")

        # Truncate if too long
        if len(content) > 100:
            content = content[:97] + "..."

        return f"✅ **Completed**: {content}"

    def _format_failure(self, todo: dict) -> str:
        """Format a failure message for a todo.

        Args:
            todo: The failed todo

        Returns:
            Formatted failure message
        """
        content = todo.get("content", "Task")

        # Truncate if too long
        if len(content) > 100:
            content = content[:97] + "..."

        return f"❌ **Failed**: {content}"
