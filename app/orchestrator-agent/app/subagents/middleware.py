"""A2A Task Tracking Middleware for deterministic context management.

This middleware extends the agent state with A2A-specific tracking fields and uses
middleware hooks to inject/extract task_id and context_id WITHOUT LLM involvement,
ensuring A2A protocol compliance and conversation continuity.

Architecture:
- Node-style hooks (before_model): Extract IDs from tool results, update state
- Wrap-style hooks (awrap_tool_call): Inject IDs, unwrap A2A metadata from responses

LangGraph Execution Flow:
  1. awrap_tool_call → injects stored task_id/context_id into sub-agent calls
  2. Tool executes → returns ToolMessage with A2A metadata
  3. awrap_tool_call → unwraps JSON-encoded metadata, stores in additional_kwargs
  4. ToolMessage gets added to messages
  5. NEXT ITERATION: before_model sees the ToolMessage ← extracts and persists IDs

Status Handling:
When A2A sub-agents return requires_input or requires_auth, the metadata is stored
in the ToolMessage's additional_kwargs. The LLM naturally sees this in the tool result
and can communicate requirements to the user (e.g., "The JIRA agent needs X").

Unlike interrupt-based approaches, this allows natural conversational flow:
  1. Tool returns with requirement metadata
  2. LLM sees requirement in tool result
  3. LLM communicates requirement to user
  4. User provides input in next turn
  5. LLM calls tool again with additional context

Unlike TodoListMiddleware which provides an LLM-controlled tool, this middleware
works transparently to maintain conversation continuity with A2A sub-agents.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Dict

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command
from langgraph.typing import ContextT
from typing_extensions import NotRequired

logger = logging.getLogger(__name__)


class A2ATrackingState(AgentState):
    """Extended agent state with A2A task tracking.

    Tracks task_id, context_id, and status for each A2A sub-agent independently,
    enabling multi-turn conversations that maintain context across calls.

    Note: These are PER-SUBAGENT tracking IDs, not the conversation-level
    task_id/context_id. The main agent's conversation tracking is separate.

    Status Handling:
    When A2A sub-agents require input or authentication, the metadata is stored
    in the tool response and visible to the LLM, which can naturally communicate
    requirements to the user.
    """

    a2a_tracking: NotRequired[Dict[str, Dict[str, Any]]]
    """Tracking data for A2A sub-agents. Format: 
    {
        "subagent_name": {
            "task_id": str,
            "context_id": str,
            "is_complete": bool,
            "requires_input": bool,
            "requires_auth": bool,
            "state": str,
            "artifacts": list
        }
    }
    """


class A2ATaskTrackingMiddleware(AgentMiddleware[A2ATrackingState, ContextT]):
    """Middleware for deterministic A2A task/context ID tracking.

    ARCHITECTURE:
    - Node-style hooks (before_model): Extract IDs and metadata from tool responses
    - Wrap-style hooks (awrap_tool_call): Inject IDs, unwrap metadata from responses

    How it works:
    1. awrap_tool_call: Injects stored task_id/context_id into sub-agent calls
    2. Tool executes and returns ToolMessage with A2A metadata
    3. awrap_tool_call: Unwraps JSON-encoded A2A metadata from response
    4. ToolMessage with metadata gets added to conversation messages
    5. NEXT ITERATION: before_model sees ToolMessage with a2a_metadata in additional_kwargs
    6. before_model: Extracts IDs from ToolMessage, returns state update
    7. LangGraph merges state update and persists via checkpointer

    Status Handling:
    - A2A metadata (requires_input, requires_auth, state) stored in ToolMessage
    - LLM sees metadata in tool result and can communicate requirements to user
    - Natural conversational flow: user provides input → LLM calls tool again
    - No interrupts needed - input consumed in subsequent tool call

    Integration:
        ```python
        agent = create_deep_agent(
            model=model,
            tools=tools,
            subagents=subagents,
            middleware=[A2ATaskTrackingMiddleware()],
            checkpointer=MemorySaver()  # Required for state persistence
        )
        ```
    """

    state_schema = A2ATrackingState

    def __init__(self):
        """Initialize the A2A tracking middleware."""
        super().__init__()

    def before_model(self, state: A2ATrackingState, runtime: Runtime[ContextT]) -> Dict[str, Any] | None:
        """Extract task_id and context_id from A2A tool responses.

        This hook runs at the START of each iteration, AFTER tool results have been
        added to messages. We examine ToolMessage results from the previous iteration
        to extract and persist A2A tracking IDs in state for the next call.

        The extraction happens here (not in awrap_tool_call) because:
        1. awrap_tool_call must return a single result (ToolMessage/Command)
        2. State updates require reducer merging handled by LangGraph
        3. before_model is the proper hook for state extraction per LangGraph docs

        Returns a dict with "a2a_tracking" key to be merged into state by LangGraph.
        """
        messages = state.get("messages", [])
        if not messages:
            return None

        # Look for ToolMessage from 'task' tool in the most recent message
        last_message = messages[-1]
        if not isinstance(last_message, ToolMessage):
            return None

        logger.info("[A2A MIDDLEWARE before_model] Found ToolMessage, checking for A2A metadata...")

        # Find the corresponding tool call to determine which subagent was invoked
        subagent_type = None
        for msg in reversed(messages[:-1]):
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if tool_call.get("id") == last_message.tool_call_id:
                        if tool_call.get("name") == "task":
                            subagent_type = tool_call.get("args", {}).get("subagent_type")
                        break
                if subagent_type:
                    break

        if not subagent_type:
            logger.debug("[A2A MIDDLEWARE before_model] Could not determine subagent_type")
            return None

        # CRITICAL ERROR DETECTION: Check for "task does not exist" error BEFORE processing metadata
        # This handles the case where the sub-agent has cleaned up a completed/failed task
        # but the orchestrator still has a stale task_id in state, causing infinite retry loops
        content = last_message.content if isinstance(last_message.content, str) else str(last_message.content)
        if "task" in content.lower() and "does not exist" in content.lower():
            logger.warning(
                f"[A2A MIDDLEWARE before_model] Detected 'task does not exist' error for {subagent_type}. "
                "Clearing stale task_id from state to prevent retry loop."
            )
            # Clear the task_id while preserving context_id and other tracking
            current_tracking = dict(state.get("a2a_tracking", {}))
            if subagent_type in current_tracking and "task_id" in current_tracking[subagent_type]:
                old_task_id = current_tracking[subagent_type]["task_id"]
                del current_tracking[subagent_type]["task_id"]
                # Also mark as incomplete to prevent re-injection
                current_tracking[subagent_type]["is_complete"] = True
                logger.info(f"[A2A MIDDLEWARE before_model] Cleared stale task_id {old_task_id} for {subagent_type}")
                return {"a2a_tracking": current_tracking}

        # Check if ToolMessage has A2A metadata in additional_kwargs
        # This is placed here by _unwrap_tool_message() after extracting from JSON response
        additional_kwargs = getattr(last_message, "additional_kwargs", {})
        a2a_metadata = additional_kwargs.get("a2a_metadata")

        if not a2a_metadata:
            logger.debug(f"[A2A MIDDLEWARE before_model] No a2a_metadata for {subagent_type}")
            return None

        task_id = a2a_metadata.get("task_id")
        context_id = a2a_metadata.get("context_id")

        if not (task_id or context_id):
            return None

        logger.info(f"[A2A MIDDLEWARE before_model] Extracting IDs for {subagent_type}:")
        logger.info(f"[A2A MIDDLEWARE before_model]   task_id: {task_id}")
        logger.info(f"[A2A MIDDLEWARE before_model]   context_id: {context_id}")

        # Build state update (don't mutate existing state - return new dict for LangGraph to merge)
        current_tracking = dict(state.get("a2a_tracking", {}))
        if subagent_type not in current_tracking:
            current_tracking[subagent_type] = {}

        # Check if task failed or completed - if so, clear task_id to start fresh next time
        # This prevents trying to reuse a task_id for a task that no longer exists
        is_complete = a2a_metadata.get("is_complete", False)
        state_str = str(a2a_metadata.get("state", ""))
        is_failed = "failed" in state_str.lower()

        # Store task_id and context_id for next invocation ONLY if task is ongoing
        # For failed/completed tasks, keep context_id but clear task_id
        if task_id and not (is_complete or is_failed):
            current_tracking[subagent_type]["task_id"] = task_id
        elif "task_id" in current_tracking[subagent_type]:
            # Clear stale task_id if task is done or failed
            logger.info(
                f"[A2A MIDDLEWARE before_model] Clearing task_id for {subagent_type} "
                f"(is_complete={is_complete}, is_failed={is_failed})"
            )
            del current_tracking[subagent_type]["task_id"]

        # Always preserve context_id for conversation continuity
        if context_id:
            current_tracking[subagent_type]["context_id"] = context_id

        # Store additional A2A protocol fields (completion status, auth requirements, etc.)
        for key in ["is_complete", "requires_auth", "requires_input", "state", "artifacts"]:
            if key in a2a_metadata:
                current_tracking[subagent_type][key] = a2a_metadata[key]

        # Return state update for LangGraph to merge via reducer
        return {"a2a_tracking": current_tracking}

    async def abefore_model(self, state: A2ATrackingState, runtime: Runtime[ContextT]) -> Dict[str, Any] | None:
        """Async version of before_model.

        Reuses the sync implementation since ID extraction is purely computational
        (no I/O or blocking operations).
        """
        return self.before_model(state, runtime)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Inject task_id/context_id and unwrap A2A JSON content (sync version).

        This wrap-style hook:
        1. Injects tracking IDs from state into tool arguments (BEFORE execution)
        2. Executes the tool via handler
        3. Unwraps JSON-encoded A2A metadata from response (AFTER execution)
        4. Stores metadata in additional_kwargs for before_model to extract

        Does NOT mutate state - before_model handles state updates.
        See awrap_tool_call for async version with interrupt-based status handling.
        """
        tool_name = request.tool_call.get("name", "")

        # Only intercept 'task' tool which invokes A2A sub-agents
        if tool_name != "task":
            return handler(request)

        # Extract the subagent_type to identify which sub-agent is being called
        args = request.tool_call.get("args", {})
        subagent_type = args.get("subagent_type")

        if not subagent_type:
            return handler(request)

        logger.info(f"[A2A MIDDLEWARE wrap_tool_call] Intercepting task call for: {subagent_type}")

        # STEP 1: Inject stored IDs from state into tool arguments for context continuity
        state = request.state
        a2a_tracking = state.get("a2a_tracking", {})
        agent_tracking = a2a_tracking.get(subagent_type, {})

        if agent_tracking:
            task_id = agent_tracking.get("task_id")
            context_id = agent_tracking.get("context_id")
            is_complete = agent_tracking.get("is_complete", True)
            requires_input = agent_tracking.get("requires_input", False)
            requires_auth = agent_tracking.get("requires_auth", False)

            # A2A Protocol: Only reuse task_id if task is incomplete, needs input, or needs auth
            task_incomplete = not is_complete or requires_input or requires_auth

            # Always inject context_id for conversation continuity
            if context_id:
                request.tool_call["args"]["context_id"] = context_id
                logger.info(f"[A2A MIDDLEWARE wrap_tool_call] Injected context_id: {context_id}")

            # Only inject task_id if the task is still in progress
            if task_id and task_incomplete:
                request.tool_call["args"]["task_id"] = task_id
                logger.info(f"[A2A MIDDLEWARE wrap_tool_call] Injected task_id: {task_id}")
            elif task_id and is_complete:
                logger.info(f"[A2A MIDDLEWARE wrap_tool_call] Task {task_id} complete, omitting task_id")
        else:
            logger.info(f"[A2A MIDDLEWARE wrap_tool_call] No tracking for {subagent_type} - new conversation")

        # STEP 2: Execute the tool with injected IDs
        result = handler(request)

        # STEP 3: Unwrap JSON-encoded A2A metadata from response and store in additional_kwargs
        if isinstance(result, ToolMessage):
            return self._unwrap_tool_message(result)
        elif isinstance(result, Command):
            return self._unwrap_command(result)

        return result

    def _unwrap_tool_message(self, tool_message: ToolMessage) -> ToolMessage:
        """Unwrap JSON-encoded A2A metadata from ToolMessage content.

        The A2A runnable wraps metadata in content as JSON:
        {"content": "actual message", "a2a": {"task_id": ..., "context_id": ...}}

        This extracts the metadata and stores it in additional_kwargs where
        before_model can find it, then returns a new ToolMessage with just
        the actual content (clean message for LLM consumption).
        """
        if not isinstance(tool_message.content, str):
            return tool_message

        try:
            content_dict = json.loads(tool_message.content)

            # Check for A2A enhanced format with embedded metadata
            if isinstance(content_dict, dict) and "content" in content_dict and "a2a" in content_dict:
                a2a_metadata = content_dict["a2a"]
                original_content = content_dict["content"]

                logger.info(f"[A2A MIDDLEWARE unwrap] Found A2A metadata: {list(a2a_metadata.keys())}")

                # Create new ToolMessage with unwrapped content and metadata in additional_kwargs
                # This allows before_model to extract IDs from a consistent, known location
                return ToolMessage(
                    content=original_content,
                    tool_call_id=tool_message.tool_call_id,
                    name=tool_message.name,
                    additional_kwargs={**tool_message.additional_kwargs, "a2a_metadata": a2a_metadata},
                )
        except json.JSONDecodeError:
            pass

        return tool_message

    def _unwrap_command(self, command: Command) -> Command:
        """Unwrap A2A metadata from Command's ToolMessage.

        When a Command is returned (containing a ToolMessage), we need to
        unwrap the ToolMessage inside it. Extracts the message from Command.update,
        unwraps it via _unwrap_tool_message, and returns updated Command.
        """
        if not (hasattr(command, "update") and command.update and "messages" in command.update):
            return command

        messages = command.update["messages"]
        if not messages or not isinstance(messages[-1], ToolMessage):
            return command

        # Unwrap the last message to extract A2A metadata
        unwrapped = self._unwrap_tool_message(messages[-1])

        # Return Command with unwrapped message and preserved goto/graph
        updated_messages = messages[:-1] + [unwrapped]
        return Command(
            update={**command.update, "messages": updated_messages},
            goto=command.goto if hasattr(command, "goto") else None,
            graph=command.graph if hasattr(command, "graph") else None,
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Async version of wrap_tool_call.

        Flow:
        1. Injects task_id/context_id from state into tool arguments
        2. Executes tool via handler
        3. Unwraps A2A JSON metadata from response
        4. Returns ToolMessage with metadata in additional_kwargs

        The metadata (requires_input, requires_auth, etc.) is visible to the LLM
        in the tool result, allowing natural communication of requirements to the user.

        Note: Auth requirements (requires_auth) are handled by auth_error_middleware
        which runs before this middleware in the stack.
        """
        tool_name = request.tool_call.get("name", "")

        # Only intercept 'task' tool which invokes A2A sub-agents
        if tool_name != "task":
            return await handler(request)

        # Extract the subagent_type to identify which sub-agent is being called
        args = request.tool_call.get("args", {})
        subagent_type = args.get("subagent_type")

        if not subagent_type:
            return await handler(request)

        logger.info(f"[A2A MIDDLEWARE awrap_tool_call] Intercepting async task call for: {subagent_type}")

        # STEP 1: Inject stored IDs from state into tool arguments for context continuity
        state = request.state
        a2a_tracking = state.get("a2a_tracking", {})
        agent_tracking = a2a_tracking.get(subagent_type, {})

        if agent_tracking:
            task_id = agent_tracking.get("task_id")
            context_id = agent_tracking.get("context_id")
            is_complete = agent_tracking.get("is_complete", True)
            requires_input = agent_tracking.get("requires_input", False)
            requires_auth = agent_tracking.get("requires_auth", False)

            # A2A Protocol: Only reuse task_id if task is incomplete, needs input, or needs auth
            task_incomplete = not is_complete or requires_input or requires_auth

            # Always inject context_id for conversation continuity
            if context_id:
                request.tool_call["args"]["context_id"] = context_id
                logger.info(f"[A2A MIDDLEWARE awrap_tool_call] Injected context_id: {context_id}")

            # Only inject task_id if the task is still in progress
            if task_id and task_incomplete:
                request.tool_call["args"]["task_id"] = task_id
                logger.info(f"[A2A MIDDLEWARE awrap_tool_call] Injected task_id: {task_id}")
            elif task_id and is_complete:
                logger.info(f"[A2A MIDDLEWARE awrap_tool_call] Task {task_id} complete, omitting task_id")
        else:
            logger.info(f"[A2A MIDDLEWARE awrap_tool_call] No tracking for {subagent_type} - new conversation")

        # STEP 2: Execute tool normally
        result = await handler(request)

        # STEP 3: Unwrap JSON-encoded A2A metadata from response
        # Error detection (e.g., "task does not exist") is handled in before_model
        # where we can actually update state properly
        if isinstance(result, ToolMessage):
            result = self._unwrap_tool_message(result)
        elif isinstance(result, Command):
            result = self._unwrap_command(result)

        # STEP 4: Return result with A2A metadata in additional_kwargs
        # The ToolMessage already contains requires_input/requires_auth in additional_kwargs
        # which the LLM can see and respond to naturally (e.g., "The agent needs X").
        #
        # Note: We don't interrupt() here because:
        # 1. The tool has already executed - interrupt would re-run the entire node
        # 2. Any input provided via Command.resume() can't be consumed by the past execution
        # 3. The LLM can naturally surface requirements to the user in the next turn
        # 4. User provides input → LLM calls tool again with additional context
        #
        # Auth requirements are still handled by auth_error_middleware (runs before this).

        return result
