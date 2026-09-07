"""A2A Task Tracking Middleware for state persistence.

This middleware extends the agent state with A2A-specific tracking fields and uses
the before_model hook to extract and persist task_id/context_id from tool responses,
ensuring A2A protocol compliance and conversation continuity.

Architecture:
- Uses ONLY before_model hook (passive observer pattern)
- Tool dispatch and JSON unwrapping handled by DynamicToolDispatchMiddleware
- This middleware simply observes ToolMessages and persists IDs to state

LangGraph Execution Flow:
  1. DynamicToolDispatchMiddleware dispatches task to subagent
  2. Subagent returns JSON-wrapped response with A2A metadata
  3. DynamicToolDispatchMiddleware unwraps JSON, puts metadata in additional_kwargs
  4. ToolMessage gets added to messages
  5. NEXT ITERATION: before_model sees ToolMessage, extracts and persists IDs

Note: The general-purpose subagent (from deepagents SubAgentMiddleware) does NOT
use A2A tracking - it's a stateless agent. Only subagents in subagent_registry
(local dynamic agents, remote A2A agents, file-analyzer) use multi-turn tracking.
"""

import logging
from typing import Any, Dict

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import NotRequired

from .task_refusal import is_concurrent_task_refusal

logger = logging.getLogger(__name__)


class A2ATrackingState(AgentState):
    """Extended agent state with A2A task tracking.

    Tracks task_id, context_id, and status for each A2A sub-agent independently,
    enabling multi-turn conversations that maintain context across calls.

    Note: These are PER-SUBAGENT tracking IDs, not the conversation-level
    task_id/context_id. The main agent's conversation tracking is separate.
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
            "state": str
        }
    }
    """


class A2ATaskTrackingMiddleware(AgentMiddleware[A2ATrackingState, ContextT]):
    """Middleware for A2A task/context ID state persistence.

    This is a PASSIVE middleware that only uses before_model to extract and persist
    A2A tracking IDs from ToolMessage responses. It does NOT intercept tool calls.

    Tool dispatch and JSON unwrapping are handled by DynamicToolDispatchMiddleware,
    which puts A2A metadata in ToolMessage.additional_kwargs["a2a_metadata"].

    How it works:
    1. DynamicToolDispatchMiddleware dispatches task and unwraps JSON response
    2. ToolMessage with a2a_metadata in additional_kwargs gets added to messages
    3. NEXT ITERATION: before_model sees ToolMessage
    4. before_model extracts IDs from additional_kwargs, returns state update
    5. LangGraph merges state update and persists via checkpointer

    Note: general-purpose subagent (from deepagents) does NOT use A2A tracking.
    Only subagents in subagent_registry use multi-turn context tracking.

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

        Returns a dict with "a2a_tracking" key to be merged into state by LangGraph.
        """
        messages = state.get("messages", [])
        if not messages:
            return None

        # EVERY ToolMessage the step just wrote, not only ``messages[-1]``.
        #
        # One step can end with several results: the orchestrator fans out
        # parallel ``task`` calls, and ToolNode writes them in ``tool_calls``
        # order. Reading only the last one dropped the a2a_metadata of every
        # earlier sibling — and with it the ``task_id`` a parked
        # ``input-required``/``auth-required`` sub-agent needs to be resumed, so
        # the next delegation to it started blank instead of continuing.
        #
        # Concurrency refusals (``DynamicToolDispatchMiddleware``) are ``task``
        # ToolMessages too and always land last, so they are skipped: they carry
        # no metadata of their own and would otherwise be the "last result".
        trailing: list[ToolMessage] = []
        for candidate in reversed(messages):
            if not isinstance(candidate, ToolMessage):
                break
            if not is_concurrent_task_refusal(candidate):
                trailing.append(candidate)
        trailing.reverse()

        if not trailing:
            return None

        logger.info(
            "[A2A MIDDLEWARE before_model] Found %d ToolMessage(s), checking for A2A metadata...", len(trailing)
        )

        # One copy for the whole step, so results from different sub-agents merge
        # instead of overwriting each other. Per-agent records are copied too: a
        # shallow ``dict()`` of the outer map would leave the nested records shared
        # with state and mutate them in place.
        current_tracking = {agent: dict(record) for agent, record in (state.get("a2a_tracking") or {}).items()}
        changed = False
        for tool_message in trailing:
            changed |= self._track_tool_message(tool_message, messages, current_tracking)

        if not changed:
            return None

        # Return state update for LangGraph to merge via reducer
        return {"a2a_tracking": current_tracking}

    @staticmethod
    def _subagent_type_for(tool_message: ToolMessage, messages: list) -> str | None:
        """Which sub-agent produced ``tool_message``, per the call that asked for it."""
        for msg in reversed(messages):
            if not (isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None)):
                continue
            for tool_call in msg.tool_calls:
                if tool_call.get("id") != tool_message.tool_call_id:
                    continue
                if tool_call.get("name") != "task":
                    return None
                return tool_call.get("args", {}).get("subagent_type")
        return None

    def _track_tool_message(self, tool_message: ToolMessage, messages: list, tracking: Dict[str, Any]) -> bool:
        """Fold one sub-agent result into ``tracking``. ``True`` if it changed anything."""
        subagent_type = self._subagent_type_for(tool_message, messages)
        if not subagent_type:
            logger.debug("[A2A MIDDLEWARE before_model] Could not determine subagent_type")
            return False

        # CRITICAL ERROR DETECTION: Check for "task does not exist" error BEFORE processing metadata
        # This handles the case where the sub-agent has cleaned up a completed/failed task
        # but the orchestrator still has a stale task_id in state, causing infinite retry loops
        content = tool_message.content if isinstance(tool_message.content, str) else str(tool_message.content)
        if "task" in content.lower() and "does not exist" in content.lower():
            # Clear the task_id while preserving context_id and other tracking.
            # No record means no stale id to clear — and nothing worth creating one for.
            agent_record = tracking.get(subagent_type) or {}
            if "task_id" in agent_record:
                # Logged here, not on the phrase alone: most results carrying it are
                # ordinary answers, and warning before knowing whether anything will
                # be cleared makes every one of them look like a stale-task error.
                logger.warning(
                    f"[A2A MIDDLEWARE before_model] Detected 'task does not exist' for {subagent_type} while "
                    "holding a task_id. Clearing it from state to prevent a retry loop."
                )
                old_task_id = agent_record.pop("task_id")
                # Also mark as incomplete to prevent re-injection
                agent_record["is_complete"] = True
                logger.info(f"[A2A MIDDLEWARE before_model] Cleared stale task_id {old_task_id} for {subagent_type}")
                return True
            # Nothing stale to clear, so DO NOT stop here: the check above is a
            # content heuristic, not an error channel, and a genuine answer can
            # contain the phrase ("that task does not exist — did you mean X?" is a
            # natural input-required park). Falling through is what the
            # single-message version did; returning here dropped the ids of a
            # first-delegation park — the very failure this middleware records them
            # for. Covered by test_a2a_tracking_step_results.py.

        # Check if ToolMessage has A2A metadata in additional_kwargs
        # This is placed by DynamicToolDispatchMiddleware after extracting from JSON response
        a2a_metadata = (getattr(tool_message, "additional_kwargs", None) or {}).get("a2a_metadata")

        if not a2a_metadata:
            logger.debug(f"[A2A MIDDLEWARE before_model] No a2a_metadata for {subagent_type}")
            return False

        task_id = a2a_metadata.get("task_id")
        context_id = a2a_metadata.get("context_id")

        if not (task_id or context_id):
            return False

        logger.info(f"[A2A MIDDLEWARE before_model] Extracting IDs for {subagent_type}:")
        logger.info(f"[A2A MIDDLEWARE before_model]   task_id: {task_id}")
        logger.info(f"[A2A MIDDLEWARE before_model]   context_id: {context_id}")

        agent_record = tracking.setdefault(subagent_type, {})

        # Check if task failed or completed - if so, clear task_id to start fresh next time
        # This prevents trying to reuse a task_id for a task that no longer exists
        is_complete = a2a_metadata.get("is_complete", False)
        state_str = str(a2a_metadata.get("state", ""))
        is_failed = "failed" in state_str.lower()

        # Store task_id and context_id for next invocation ONLY if task is ongoing
        # For failed/completed tasks, keep context_id but clear task_id
        if task_id and not (is_complete or is_failed):
            agent_record["task_id"] = task_id
        elif "task_id" in agent_record:
            # Clear stale task_id if task is done or failed
            logger.info(
                f"[A2A MIDDLEWARE before_model] Clearing task_id for {subagent_type} "
                f"(is_complete={is_complete}, is_failed={is_failed})"
            )
            del agent_record["task_id"]

        # Always preserve context_id for conversation continuity
        if context_id:
            agent_record["context_id"] = context_id

        # Store additional A2A protocol fields (completion status, auth requirements, etc.)
        # Including foundry_session_rid for Foundry agents' session continuity
        for key in ["is_complete", "requires_auth", "requires_input", "state", "foundry_session_rid"]:
            if key in a2a_metadata:
                agent_record[key] = a2a_metadata[key]

        return True

    async def abefore_model(self, state: A2ATrackingState, runtime: Runtime[ContextT]) -> Dict[str, Any] | None:
        """Async version of before_model.

        Reuses the sync implementation since ID extraction is purely computational
        (no I/O or blocking operations).
        """
        return self.before_model(state, runtime)
