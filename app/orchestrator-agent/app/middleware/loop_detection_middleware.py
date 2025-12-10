"""Loop Detection Middleware for preventing infinite tool call loops.

This middleware tracks tool calls in a sliding window and detects when the same tool
with identical arguments is being called repeatedly without progress, indicating a
potential infinite loop.

Key Features:
- Tracks tool calls (name + arguments hash) in a sliding window
- Detects repeated identical tool calls (configurable threshold)
- Uses interrupt() to pause execution and request user confirmation
- Provides clear feedback about the detected loop pattern

Architecture:
- Wrap-style hooks: Intercept tool calls to track patterns
- State-based tracking: Uses agent state to maintain call history
- Interrupt on detection: Uses LangGraph's interrupt() mechanism

Integration:
    ```python
    agent = create_deep_agent(
        model=model,
        tools=tools,
        middleware=[
            DynamicToolDispatchMiddleware(),
            RepeatedToolCallMiddleware(max_repeats=3, window_size=10),
            AuthErrorDetectionMiddleware(),
        ],
        checkpointer=MemorySaver()
    )
    ```
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from a2a.types import TaskState
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT
from typing_extensions import NotRequired

logger = logging.getLogger(__name__)


class LoopDetectionState(AgentState):
    """Extended agent state with tool call tracking for loop detection.

    Tracks recent tool calls to detect repeated patterns that indicate loops.
    """

    tool_call_history: NotRequired[list[dict[str, Any]]]
    """History of recent tool calls for loop detection. Format:
    [
        {
            "tool": "tool_name",
            "args_hash": "hash_of_args",
            "timestamp": float,
            "result_summary": "first_100_chars_of_result"
        }
    ]
    """


class RepeatedToolCallMiddleware(AgentMiddleware[LoopDetectionState, ContextT]):
    """Middleware for detecting and preventing infinite tool call loops.

    ARCHITECTURE:
    This middleware uses a sliding window to track recent tool calls and detects
    when the same tool with identical arguments is being called repeatedly.

    How it works:
    1. awrap_tool_call: Intercept tool executions
    2. Tool executes and returns ToolMessage
    3. Extract tool name and hash arguments for comparison
    4. Add to sliding window history (limited to window_size)
    5. Check if same tool+args appears more than max_repeats times
    6. If loop detected: Call interrupt() with clear message
    7. User can choose to continue or stop

    Configuration:
    - max_repeats: Number of identical calls before triggering (default: 3)
    - window_size: Size of sliding window to track (default: 10)

    Interrupt Value Format:
    {
        "task_state": TaskState.input_required,
        "message": "Detected repeated tool calls...",
        "interrupt_reason": "repeated_tool_calls",
        "tool": "tool_name",
        "repeat_count": 4,
        "pattern": "ls() called 4 times with same arguments"
    }
    """

    def __init__(self, max_repeats: int = 3, window_size: int = 10):
        """Initialize loop detection middleware.

        Args:
            max_repeats: Number of identical tool calls before interrupting (default: 3)
            window_size: Size of sliding window for tracking calls (default: 10)
        """
        self.max_repeats = max_repeats
        self.window_size = window_size
        logger.info(f"RepeatedToolCallMiddleware initialized: max_repeats={max_repeats}, window_size={window_size}")

    def _hash_args(self, args: dict[str, Any]) -> str:
        """Create a stable hash of tool arguments for comparison.

        Args:
            args: Tool call arguments dictionary

        Returns:
            SHA256 hash of sorted JSON representation
        """
        try:
            # Sort keys for stable hashing
            args_json = json.dumps(args, sort_keys=True)
            return hashlib.sha256(args_json.encode()).hexdigest()[:16]
        except (TypeError, ValueError) as e:
            # Fallback for non-serializable args
            logger.warning(f"Failed to hash args: {e}, using str representation")
            return hashlib.sha256(str(args).encode()).hexdigest()[:16]

    def _check_for_loop(self, tool_name: str, args_hash: str, history: list[dict[str, Any]]) -> tuple[bool, int]:
        """Check if current tool call indicates a repeated loop pattern.

        Args:
            tool_name: Name of the tool being called
            args_hash: Hash of the tool arguments
            history: Recent tool call history

        Returns:
            Tuple of (is_loop_detected, repeat_count)
        """
        # Count how many times this exact tool+args combination appears in history
        repeat_count = sum(1 for call in history if call["tool"] == tool_name and call["args_hash"] == args_hash)

        # Check if we've exceeded the threshold
        is_loop = repeat_count >= self.max_repeats

        if is_loop:
            logger.warning(
                f"Loop detected: {tool_name} with args_hash={args_hash} called {repeat_count} times "
                f"(threshold: {self.max_repeats})"
            )

        return is_loop, repeat_count

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Track tool calls and detect repeated patterns indicating loops.

        This async wrap-style hook:
        1. Retrieves current tool call history from state
        2. Executes the tool via handler
        3. Checks if this tool+args combination has been called too many times
        4. If loop detected: Interrupts execution with user prompt
        5. Records the call in history (sliding window)

        Args:
            request: Tool call request with tool name and arguments
            handler: Async callback to execute the tool

        Returns:
            ToolMessage or Command from tool execution, or interrupt if loop detected
        """
        from langgraph.types import interrupt

        tool_name = request.tool_call.get("name", "")
        args = request.tool_call.get("args", {})
        args_hash = self._hash_args(args)

        # Get current history from state (initialize if not present)
        state = request.runtime.state
        history = list(state.get("tool_call_history", []))

        # Check for loop BEFORE executing the tool
        is_loop, repeat_count = self._check_for_loop(tool_name, args_hash, history)

        if is_loop:
            # Build detailed message about the loop pattern
            # Find all matching calls to show the pattern
            matching_calls = [call for call in history if call["tool"] == tool_name and call["args_hash"] == args_hash]

            pattern_desc = f"'{tool_name}' called {repeat_count} times with identical arguments"
            if matching_calls:
                # Show result summaries to indicate lack of progress
                results = [call.get("result_summary", "")[:50] for call in matching_calls[-3:]]
                pattern_desc += f". Recent results: {results}"

            interrupt_value = {
                "task_state": TaskState.input_required,
                "message": (
                    f"⚠️ **Loop Detected**: The tool `{tool_name}` has been called {repeat_count} times "
                    f"with the same arguments, which may indicate an infinite loop or unproductive repetition.\n\n"
                    f"**Pattern**: {pattern_desc}\n\n"
                    f"This often happens when:\n"
                    f"- A tool returns empty results but the agent keeps retrying\n"
                    f"- The agent doesn't understand the tool's response\n"
                    f"- The agent is stuck in a decision loop\n\n"
                    f"Would you like to:\n"
                    f"- **Continue**: Reply 'yes' or 'continue' to allow more attempts\n"
                    f"- **Stop**: Reply 'stop' or provide different instructions"
                ),
                "interrupt_reason": "repeated_tool_calls",
                "tool": tool_name,
                "repeat_count": repeat_count,
                "pattern": pattern_desc,
            }

            logger.info(f"[LOOP DETECTION] Interrupting due to repeated tool calls: {tool_name}")
            interrupt(interrupt_value)

            # This line shouldn't be reached, but return for safety
            # Execute the tool anyway in case interrupt is bypassed
            result = await handler(request)
        else:
            # Execute the tool normally
            result = await handler(request)

        # Record this call in history (sliding window)
        result_summary = ""
        if isinstance(result, ToolMessage):
            content = result.content if isinstance(result.content, str) else str(result.content)
            result_summary = content[:100]  # First 100 chars for pattern matching

        call_record = {
            "tool": tool_name,
            "args_hash": args_hash,
            "result_summary": result_summary,
        }

        # Maintain sliding window (keep only last window_size calls)
        history.append(call_record)
        if len(history) > self.window_size:
            history = history[-self.window_size :]

        # Update state with new history
        # Note: This assumes the state will be updated by the framework
        # The state update happens automatically in the agent loop
        request.runtime.state["tool_call_history"] = history

        return result
