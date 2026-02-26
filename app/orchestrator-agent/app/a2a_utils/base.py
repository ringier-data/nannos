"""Base classes for A2A Runnable implementations.

Provides abstract base class and shared utilities for both remote (A2A protocol)
and local (in-process) sub-agents, ensuring consistent response formats.

Design Principles:
1. All sub-agents return the same response format for middleware compatibility
2. Shared `_wrap_message_with_metadata` ensures consistent JSON structure
3. Abstract interface allows type-safe usage across the codebase
"""

import json
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SubAgentInput(BaseModel):
    """Input data structure for sub-agent execution.

    This is the standardized input format that all A2A runnables expect.
    """

    a2a_tracking: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    messages: List[HumanMessage]
    orchestrator_conversation_id: Optional[str] = Field(
        default=None,
        description="Orchestrator's conversation ID for unified tracking across all sub-agents",
    )


class BaseA2ARunnable(ABC):
    """Abstract base class for A2A Runnables.

    Defines the common interface and shared utilities for both remote
    (A2A protocol) and local (in-process) sub-agents.

    All sub-agents must return responses in a consistent format:
    {
        "messages": [AIMessage(content=json_wrapped_content)],
        "task_id": "...",
        "context_id": "...",
        "state": "completed|failed|input_required|...",
        "is_complete": bool,
        "requires_input": bool,
        "requires_auth": bool,
        ...additional metadata...
    }

    The message content is always JSON-wrapped to embed A2A metadata:
    {
        "content": "actual response text",
        "a2a": {
            "task_id": "...",
            "context_id": "...",
            ...
        }
    }
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the agent name"""
        ...

    @property
    @abstractmethod
    def input_modes(self) -> List[str]:
        """Return the list of input modalities supported by this agent (e.g. ['text', 'image', 'file'])"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Return the agent description use for agent selection."""
        ...

    @abstractmethod
    async def ainvoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Async invoke the sub-agent.

        Args:
            input_data: Input data matching SubAgentInput schema

        Returns:
            Dict with 'messages', 'task_id', 'context_id', 'state', etc.
        """
        ...

    def invoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Synchronous invoke wrapper.

        Default implementation runs ainvoke in a new event loop.
        Override if sync execution is supported natively.

        Args:
            input_data: Input data matching SubAgentInput schema

        Returns:
            Dict with 'messages', 'task_id', 'context_id', 'state', etc.
        """
        import asyncio

        return asyncio.run(self.ainvoke(input_data))

    def _wrap_message_with_metadata(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Wrap the result message with A2A metadata embedded in content.

        The deepagents library strips additional_kwargs when creating ToolMessage,
        so we embed metadata directly in the content as JSON.

        This method should be called on every response before returning to
        ensure the middleware can extract A2A metadata.

        Args:
            result: Result dictionary containing messages and metadata

        Returns:
            Updated result with wrapped message
        """
        if not result.get("messages"):
            # Create synthetic message if none exists
            content = "Task processed"
            if "input_prompt" in result:
                content = result["input_prompt"]
            elif "error_message" in result:
                content = f"Error: {result['error_message']}"
            elif "responses" in result and result["responses"]:
                content = result["responses"][-1] if result["responses"] else "Processing complete"

            result["messages"] = [AIMessage(content=content)]
            logger.debug(f"Created synthetic message: {content}")

        # Wrap last message with metadata
        last_message = result["messages"][-1]
        if isinstance(last_message, AIMessage):
            a2a_metadata = {
                k: v
                for k, v in {
                    "task_id": result.get("task_id"),
                    "context_id": result.get("context_id"),
                    "is_complete": result.get("is_complete"),
                    "requires_auth": result.get("requires_auth"),
                    "requires_input": result.get("requires_input"),
                    "state": str(result.get("state")) if result.get("state") else None,
                    "artifacts": result.get("artifacts"),
                    "foundry_session_rid": result.get("foundry_session_rid"),
                }.items()
                if v is not None
            }

            wrapped_content = {"content": last_message.content, "a2a": a2a_metadata}

            result["messages"][-1] = AIMessage(content=json.dumps(wrapped_content))
            logger.debug(f"Wrapped message with metadata: task_id={a2a_metadata.get('task_id')}")

        return result

    def _build_response(
        self,
        content: str,
        *,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
        state: str = "completed",
        requires_input: bool = False,
        requires_auth: bool = False,
        artifacts: Optional[List[Dict[str, Any]]] = None,
        **extra_metadata: Any,
    ) -> Dict[str, Any]:
        """Build a structured response in A2A-compatible format.

        Creates a response dict with 'messages' and top-level metadata fields,
        ensuring consistent format across all sub-agent implementations.

        Args:
            content: The message content
            task_id: Unique ID for this task (generated if not provided)
            context_id: Persistent ID for conversation continuity
            state: Task state (completed, input_required, failed, etc.)
            requires_input: Whether user/orchestrator input is needed
            requires_auth: Whether authentication is required
            artifacts: Optional list of artifacts
            **extra_metadata: Additional metadata to include at top level

        Returns:
            Dict with 'messages' list and A2A metadata fields at top level
        """
        # Generate IDs if not provided
        if task_id is None:
            task_id = str(uuid.uuid4())
        if context_id is None:
            context_id = str(uuid.uuid4())

        # Build response dict (plain, no JSON wrapping)
        # Wrapping will be done by _wrap_message_with_metadata() in ainvoke()
        response = {
            "messages": [AIMessage(content=content)],
            "task_id": task_id,
            "context_id": context_id,
            "state": state,
            "is_complete": state == "completed",
            "requires_input": requires_input,
            "requires_auth": requires_auth,
            **extra_metadata,
        }

        if artifacts:
            response["artifacts"] = artifacts

        return response

    def _build_error_response(
        self,
        message: str,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
        **extra_metadata: Any,
    ) -> Dict[str, Any]:
        """Build an error/failed response.

        Args:
            message: Error description
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID
            **extra_metadata: Additional metadata to include at top level

        Returns:
            Dict with messages and A2A metadata indicating failure
        """
        return self._build_response(
            message,
            context_id=context_id,
            task_id=task_id,
            state="failed",
            requires_input=False,
            **extra_metadata,
        )

    def _build_input_required_response(
        self,
        message: str,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
        **extra_metadata: Any,
    ) -> Dict[str, Any]:
        """Build an input_required response for the orchestrator to handle.

        Args:
            message: Explanation of what input is needed
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID
            **extra_metadata: Additional metadata to include at top level

        Returns:
            Dict with messages and A2A metadata indicating input is required
        """
        return self._build_response(
            message,
            context_id=context_id,
            task_id=task_id,
            state="input_required",
            requires_input=True,
            **extra_metadata,
        )

    def _build_success_response(
        self,
        content: str,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
        artifacts: Optional[List[Dict[str, Any]]] = None,
        **extra_metadata: Any,
    ) -> Dict[str, Any]:
        """Build a successful completion response.

        Args:
            content: The result content
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID
            artifacts: Optional list of artifacts
            **extra_metadata: Additional metadata to include at top level

        Returns:
            Dict with messages and A2A metadata indicating completion
        """
        return self._build_response(
            content,
            context_id=context_id,
            task_id=task_id,
            state="completed",
            requires_input=False,
            artifacts=artifacts,
            **extra_metadata,
        )

    def _extract_message_content(self, input_data: SubAgentInput) -> str:
        """Extract and prepare message content from input data.

        Args:
            input_data: Validated input data containing messages

        Returns:
            Extracted content as string

        Raises:
            ValueError: If no content is provided
        """
        if not input_data.messages:
            raise ValueError(f"No messages provided. Input data: {input_data}")

        raw_content = input_data.messages[-1].content
        if not raw_content:
            raise ValueError(f"No input content provided. Input data: {input_data}")

        # Convert to string if needed
        if isinstance(raw_content, str):
            return raw_content

        logger.debug("Converting non-string content to JSON")
        return json.dumps(raw_content[-1])

    def _extract_tracking_ids(self, input_data: SubAgentInput) -> tuple[Optional[str], Optional[str]]:
        """Extract context_id and task_id from a2a_tracking state with orchestrator fallback.

        Implements waterfall pattern for conversation ID propagation:
        1. Use sub-agent's persisted context_id from a2a_tracking (for follow-up calls)
        2. Fallback to orchestrator's conversation_id (for first call to this sub-agent)

        This enables unified conversation tracking across all agents (local and remote).
        For remote agents, the context_id is propagated via the standard A2A Message.context_id
        field, ensuring native protocol compliance.

        Args:
            input_data: Input data containing a2a_tracking and orchestrator_conversation_id

        Returns:
            Tuple of (context_id, task_id). task_id is only returned if the task
            is incomplete or requires user intervention (auth/input).
        """
        logger.debug(f"Extracting tracking IDs for agent: {self.name}")
        logger.debug(f"Full a2a_tracking state: {input_data.a2a_tracking}")
        agent_name = self.name.replace(" ", "")
        agent_tracking = input_data.a2a_tracking.get(agent_name, {})

        # Waterfall: Try persisted context_id first, fallback to orchestrator's
        context_id = agent_tracking.get("context_id") if agent_tracking else None

        if not context_id and input_data.orchestrator_conversation_id:
            # First call to this sub-agent: use orchestrator's conversation ID
            context_id = input_data.orchestrator_conversation_id
            logger.info(
                f"[CONVERSATION_ID] First call to '{agent_name}': using orchestrator conversation_id={context_id}"
            )
        elif context_id:
            logger.debug(f"[CONVERSATION_ID] Follow-up call to '{agent_name}': using persisted context_id={context_id}")
        else:
            logger.debug(
                f"No tracking found for agent: {agent_name}. Available: {list(input_data.a2a_tracking.keys())}"
            )

        task_id = agent_tracking.get("task_id") if agent_tracking else None
        is_complete = agent_tracking.get("is_complete", True) if agent_tracking else True

        # Always return context_id for conversation continuity
        # Only return task_id if the task is still in progress
        if task_id and is_complete:
            logger.debug(f"Task {task_id} complete, omitting task_id for new request")
            task_id = None

        return context_id, task_id


class LocalA2ARunnable(BaseA2ARunnable):
    """Base class for local (in-process) A2A sub-agents.

    Provides a simpler interface for sub-agents that don't need
    network communication - they execute within the same process.

    Subclasses should:
    1. Override the `name` property
    2. Implement `_process` method with the actual logic
    3. Use `_build_*_response` helpers to format responses

    Example:
        class MyLocalAgent(LocalA2ARunnable):
            @property
            def name(self) -> str:
                return "my-agent"

            async def _process(
                self,
                content: str,
                context_id: Optional[str],
            ) -> Dict[str, Any]:
                result = await do_something(content)
                return self._build_success_response(result, context_id)
    """

    @abstractmethod
    async def _process(self, input_data: SubAgentInput) -> Dict[str, Any]:
        """Process the input and return a response.

        Subclasses implement this method with their specific logic.
        Use `_build_*_response` helpers to format the response.

        Args:
            input_data: Validated input with messages, a2a_tracking, and files

        Returns:
            Dict with 'messages' and A2A metadata (plain, will be wrapped by ainvoke)

        Example:
            async def _process(self, input_data: SubAgentInput) -> Dict[str, Any]:
                content = self._extract_message_content(input_data)
                context_id, task_id = self._extract_tracking_ids(input_data)

                # Access a2a_tracking for custom state
                custom_state = input_data.a2a_tracking.get(self.name, {})

                result = await do_something(content)
                return self._build_success_response(result, context_id=context_id)
        """
        ...

    async def ainvoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Async invoke the local sub-agent.

        Validates input, delegates to _process with full SubAgentInput,
        then wraps the response with A2A metadata.

        Args:
            input_data: Input data matching SubAgentInput schema

        Returns:
            Dict with 'messages' containing JSON-wrapped content and A2A metadata
        """
        try:
            # Validate input
            validated = SubAgentInput.model_validate(input_data)

            # Delegate to subclass implementation with full input data
            result = await self._process(validated)

            # Wrap message with A2A metadata (same as A2AClientRunnable)
            return self._wrap_message_with_metadata(result)

        except ValueError as e:
            # Content extraction errors
            result = self._build_error_response(str(e))
            return self._wrap_message_with_metadata(result)
        except Exception as e:
            logger.exception(f"Error in {self.name}: {e}")
            result = self._build_error_response(f"Internal error: {str(e)}")
            return self._wrap_message_with_metadata(result)
