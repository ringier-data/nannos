"""Loop Detection Middleware for preventing infinite tool call loops.

This middleware tracks tool calls in a sliding window and detects when the same tool
with identical arguments is being called repeatedly without progress, indicating a
potential infinite loop.

Key Features:
- Tracks tool calls (name + arguments hash) in a sliding window
- Detects repeated identical tool calls (configurable threshold)
- Blocks looping tool calls with error ToolMessages (like ToolCallLimitMiddleware)
- Allows non-looping tool calls to execute normally
- Provides clear feedback about the detected loop pattern

Architecture:
- after_model hook: Checks for loops after model generates tool calls
- State-based tracking: Uses agent state to maintain call history
- Selective blocking: Only blocks looping tool calls, lets others execute

Integration:
    ```python
    agent = create_deep_agent(
        model=model,
        tools=tools,
        middleware=[
            DynamicToolDispatchMiddleware(),
            RepeatedToolCallMiddleware(max_repeats=3, max_tool_repeats=5, window_size=10),
            AuthErrorDetectionMiddleware(),
        ],
        checkpointer=MemorySaver()
    )
    ```
"""

import hashlib
import json
import logging
from typing import Annotated, Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, PrivateStateAttr
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import NotRequired

logger = logging.getLogger(__name__)


class LoopDetectionState(AgentState):
    """Extended agent state with tool call tracking for loop detection.

    Tracks tool call history to detect repeated patterns per tool.
    Similar to ToolCallLimitMiddleware but tracks both same-args and same-tool patterns.
    """

    tool_call_history: NotRequired[Annotated[dict[str, list[str]], PrivateStateAttr]]
    """Per-tool history of argument hashes. Format:
    {
        "tool_name": ["args_hash1", "args_hash2", ...],
        ...
    }
    """


class RepeatedToolCallMiddleware(AgentMiddleware[LoopDetectionState, ContextT]):
    """Middleware for detecting and preventing infinite tool call loops.

    Extends ToolCallLimitMiddleware pattern with dual loop detection:
    1. Same tool + same arguments (max_repeats threshold)
    2. Same tool regardless of arguments (max_tool_repeats threshold)

    Unlike ToolCallLimitMiddleware which tracks absolute counts, this tracks:
    - Per-tool history of argument hashes in a sliding window
    - Blocks tool calls that exceed either threshold
    - Injects error ToolMessages for blocked calls
    - Lets non-looping calls execute normally

    The model receives error messages and can adjust its strategy accordingly.

    Configuration:
    - max_repeats: Same tool+args threshold before blocking (default: 3)
    - max_tool_repeats: Same tool (any args) threshold before blocking (default: 5)
    - window_size: Sliding window size for history tracking (default: 10)
    - tool_name: Specific tool to track, or None for all tools (default: None)
    """

    state_schema = LoopDetectionState  # type: ignore[assignment]

    def __init__(
        self,
        *,
        tool_name: str | None = None,
        max_repeats: int = 3,
        max_tool_repeats: int = 5,
        window_size: int = 10,
    ):
        """Initialize loop detection middleware.

        Args:
            tool_name: Specific tool to track. If None, tracks all tools.
            max_repeats: Same tool+args threshold (default: 3)
            max_tool_repeats: Same tool threshold (default: 5)
            window_size: Sliding window size (default: 10)
        """
        super().__init__()
        self.tool_name = tool_name
        self.max_repeats = max_repeats
        self.max_tool_repeats = max_tool_repeats
        self.window_size = window_size
        logger.info(
            f"RepeatedToolCallMiddleware initialized: tool_name={tool_name}, "
            f"max_repeats={max_repeats}, max_tool_repeats={max_tool_repeats}, window_size={window_size}"
        )

    @property
    def name(self) -> str:
        """The name of the middleware instance."""
        base_name = self.__class__.__name__
        if self.tool_name:
            return f"{base_name}[{self.tool_name}]"
        return base_name

    def _hash_args(self, args: dict[str, Any]) -> str:
        """Create a stable hash of tool arguments for comparison.

        Args:
            args: Tool call arguments dictionary

        Returns:
            SHA256 hash of sorted JSON representation
        """
        try:
            args_json = json.dumps(args, sort_keys=True)
            return hashlib.sha256(args_json.encode()).hexdigest()[:16]
        except (TypeError, ValueError) as e:
            logger.warning(f"Failed to hash args: {e}, using str representation")
            return hashlib.sha256(str(args).encode()).hexdigest()[:16]

    def _matches_tool_filter(self, tool_call: ToolCall) -> bool:
        """Check if a tool call matches this middleware's tool filter.

        Args:
            tool_call: The tool call to check.

        Returns:
            True if this middleware should track this tool call.
        """
        return self.tool_name is None or tool_call["name"] == self.tool_name

    def _check_for_loop(self, tool_name: str, args_hash: str, tool_history: list[str]) -> tuple[bool, int, str]:
        """Check if adding this call would create a loop pattern.

        Args:
            tool_name: Name of the tool being called
            args_hash: Hash of the tool arguments
            tool_history: List of arg hashes for this specific tool

        Returns:
            Tuple of (is_loop_detected, repeat_count, loop_type)
            where loop_type is 'same_args' or 'same_tool'
        """
        # Count same arguments (+1 for current call)
        same_args_count = tool_history.count(args_hash) + 1

        # Count all calls to this tool (+1 for current call)
        same_tool_count = len(tool_history) + 1

        # Check thresholds
        if same_args_count > self.max_repeats:
            logger.warning(
                f"Loop detected: {tool_name} with args_hash={args_hash} "
                f"called {same_args_count} times (threshold: {self.max_repeats})"
            )
            return True, same_args_count, "same_args"

        if same_tool_count > self.max_tool_repeats:
            unique_args = len(set(tool_history + [args_hash]))
            logger.warning(
                f"Loop detected: {tool_name} called {same_tool_count} times "
                f"with {unique_args} unique arg sets (threshold: {self.max_tool_repeats})"
            )
            return True, same_tool_count, "same_tool"

        return False, 0, ""

    async def aafter_model(
        self,
        state: LoopDetectionState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Check tool calls for loop patterns and block looping calls.

        Follows ToolCallLimitMiddleware pattern:
        - Only blocks looping tool calls with error ToolMessages
        - Lets non-looping calls execute normally
        - Updates history state for tracking

        Args:
            state: Current agent state
            runtime: LangGraph runtime context

        Returns:
            State updates with history and error messages for blocked calls
        """
        # Get the last AIMessage
        messages = state.get("messages", [])
        if not messages:
            return None

        last_ai_message = None
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                last_ai_message = message
                break

        if not last_ai_message or not last_ai_message.tool_calls:
            return None

        # Get current history (per-tool tracking)
        history = state.get("tool_call_history", {}).copy()

        # Track blocked calls and update history
        blocked_calls: list[dict[str, Any]] = []

        for tool_call in last_ai_message.tool_calls:
            tool_name = tool_call["name"]

            # Skip if doesn't match filter
            if not self._matches_tool_filter(tool_call):
                continue

            args = tool_call.get("args", {})
            args_hash = self._hash_args(args)

            # Get this tool's history
            tool_history = history.get(tool_name, [])

            # Check for loop
            is_loop, repeat_count, loop_type = self._check_for_loop(tool_name, args_hash, tool_history)

            if is_loop:
                # Build description
                if loop_type == "same_args":
                    same_count = tool_history.count(args_hash)
                    desc = f"Tool '{tool_name}' called {same_count} times with identical arguments"
                else:  # same_tool
                    unique_count = len(set(tool_history))
                    desc = (
                        f"Tool '{tool_name}' called {repeat_count} times (with {unique_count} different argument sets)"
                    )

                blocked_calls.append(
                    {
                        "tool_call": tool_call,
                        "tool_name": tool_name,
                        "loop_type": loop_type,
                        "description": desc,
                        "repeat_count": repeat_count,
                    }
                )

                logger.warning(f"Loop detected: {desc}")

                # CRITICAL: Add blocked calls to history so count increases
                # This provides escalating feedback to the model
                tool_history.append(args_hash)
            else:
                # Not looping - add to history
                tool_history.append(args_hash)

            # Always update history (for both looping and non-looping)
            # Maintain sliding window
            if len(tool_history) > self.window_size:
                tool_history = tool_history[-self.window_size :]

            history[tool_name] = tool_history

        # If no blocked calls, just update history
        if not blocked_calls:
            logger.debug(f"[LOOP DETECTION] Tracked {len(last_ai_message.tool_calls)} tool call(s)")
            return {"tool_call_history": history}

        # Build error ToolMessages for blocked calls (only blocked ones!)
        # Use very strong, explicit language for GPT-4o-mini
        error_messages = [
            ToolMessage(
                content=(
                    f"❌ BLOCKED: {info['description']}. "
                    f"This tool has been called {info['repeat_count']} times and is NOT working. "
                    f"STOP trying '{info['tool_name']}'. "
                    f"You MUST use a completely different approach or give up. "
                    f"Continuing to call this tool will fail."
                ),
                tool_call_id=info["tool_call"]["id"],
                name=info["tool_name"],
                status="error",
            )
            for info in blocked_calls
        ]

        logger.info(
            f"[LOOP DETECTION] Blocked {len(blocked_calls)} looping tool call(s): "
            f"{[info['tool_name'] for info in blocked_calls]}"
        )

        # Return updated history and error messages
        return {
            "tool_call_history": history,
            "messages": error_messages,
        }
