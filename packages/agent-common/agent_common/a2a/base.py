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
from collections.abc import AsyncIterable
from typing import Any, Dict, List, Optional

from a2a.types import TaskState
from langchain_core.messages import AIMessage, ContentBlock, HumanMessage
from pydantic import BaseModel, Field
from ringier_a2a_sdk.agent.cost_tracking_mixin import CostTrackingMixin
from ringier_a2a_sdk.utils.bedrock_image_processor import preprocess_content_blocks_for_bedrock

from agent_common.core.model_factory import get_model_backend

from .stream_events import ErrorEvent, StreamEvent, TaskResponseData, TaskUpdate

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
    scheduled_job_id: Optional[int] = Field(
        default=None,
        description="Scheduled job ID to propagate to remote agents for cost attribution.",
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
    def description(self) -> str:
        """Return the agent description use for agent selection."""
        ...

    async def ainvoke(self, input_data: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> StreamEvent:
        """Async invoke the sub-agent by collecting stream results.

        Delegates to astream() and collects the final StreamEvent, ensuring
        a single code path for both streaming and non-streaming invocations.

        Args:
            input_data: Input data matching SubAgentInput schema
            config: Optional RunnableConfig for LangChain callback/tracing propagation

        Returns:
            The final StreamEvent (TaskUpdate or ErrorEvent)
        """
        last_event: StreamEvent | None = None
        async for item in self.astream(input_data, config):
            last_event = item
        if last_event is None:
            return ErrorEvent(error="No response received from agent")
        return last_event

    def invoke(self, input_data: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> StreamEvent:
        """Synchronous invoke wrapper.

        Default implementation runs ainvoke in a new event loop.
        Override if sync execution is supported natively.

        Args:
            input_data: Input data matching SubAgentInput schema

        Returns:
            The final StreamEvent (TaskUpdate or ErrorEvent)
        """
        import asyncio

        return asyncio.run(self.ainvoke(input_data, config))

    def _wrap_message_with_metadata(self, result: TaskResponseData) -> TaskResponseData:
        """Wrap the result message with A2A metadata embedded in content.

        The deepagents library strips additional_kwargs when creating ToolMessage,
        so we embed metadata directly in the content as JSON.

        This method should be called on every response before returning to
        ensure the middleware can extract A2A metadata.

        Args:
            result: TaskResponseData containing messages and metadata

        Returns:
            Updated result with wrapped message
        """
        if not result.messages:
            # Create synthetic message if none exists
            content = "Task processed"
            meta = result.metadata
            if "input_prompt" in meta:
                content = meta["input_prompt"]
            elif "error_message" in meta:
                content = f"Error: {meta['error_message']}"
            elif "responses" in meta and meta["responses"]:
                content = meta["responses"][-1] if meta["responses"] else "Processing complete"

            result.messages = [AIMessage(content=content)]
            logger.debug(f"Created synthetic message: {content}")

        # Wrap last message with metadata
        last_message = result.messages[-1]
        if isinstance(last_message, AIMessage):
            a2a_metadata = {
                k: v
                for k, v in {
                    "task_id": result.task_id,
                    "context_id": result.context_id,
                    "is_complete": result.is_complete,
                    "requires_auth": result.requires_auth,
                    "requires_input": result.requires_input,
                    "state": result.state.value if result.state else None,
                    **{k: v for k, v in result.metadata.items() if v is not None},
                }.items()
                if v is not None
            }

            wrapped_content = {"content": last_message.content, "a2a": a2a_metadata}

            result.messages[-1] = AIMessage(content=json.dumps(wrapped_content))
            logger.debug(f"Wrapped message with metadata: task_id={a2a_metadata.get('task_id')}")

        return result

    def _build_response(
        self,
        content: str,
        *,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
        state: TaskState = TaskState.completed,
        **extra_metadata: Any,
    ) -> TaskResponseData:
        """Build a structured response in A2A-compatible format.

        Creates a ``TaskResponseData`` with typed lifecycle fields and an
        extensible ``metadata`` dict, following the A2A protocol pattern.

        Args:
            content: The message content
            task_id: Unique ID for this task (generated if not provided)
            context_id: Persistent ID for conversation continuity
            state: Task state (``TaskState`` enum)
            **extra_metadata: Additional metadata to include in ``metadata``

        Returns:
            TaskResponseData with typed core fields and extra metadata
        """
        if task_id is None:
            task_id = str(uuid.uuid4())
        if context_id is None:
            context_id = str(uuid.uuid4())

        return TaskResponseData(
            task_id=task_id,
            context_id=context_id,
            state=state,
            messages=[AIMessage(content=content)],
            metadata=extra_metadata if extra_metadata else {},
        )

    def _build_error_response(
        self,
        message: str,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
        **extra_metadata: Any,
    ) -> TaskResponseData:
        """Build an error/failed response.

        Args:
            message: Error description
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID
            **extra_metadata: Additional metadata to include in ``metadata``

        Returns:
            TaskResponseData indicating failure
        """
        return self._build_response(
            message,
            context_id=context_id,
            task_id=task_id,
            state=TaskState.failed,
            **extra_metadata,
        )

    def _build_input_required_response(
        self,
        message: str,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
        **extra_metadata: Any,
    ) -> TaskResponseData:
        """Build an input_required response for the orchestrator to handle.

        Args:
            message: Explanation of what input is needed
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID
            **extra_metadata: Additional metadata to include in ``metadata``

        Returns:
            TaskResponseData indicating input is required
        """
        return self._build_response(
            message,
            context_id=context_id,
            task_id=task_id,
            state=TaskState.input_required,
            **extra_metadata,
        )

    def _build_success_response(
        self,
        content: str,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
        **extra_metadata: Any,
    ) -> TaskResponseData:
        """Build a successful completion response.

        Args:
            content: The result content
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID
            **extra_metadata: Additional metadata to include in ``metadata``

        Returns:
            TaskResponseData indicating completion
        """
        return self._build_response(
            content,
            context_id=context_id,
            task_id=task_id,
            state=TaskState.completed,
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

    @staticmethod
    def _ensure_supported_block_types(file_blocks: list, supported_modes: Optional[List[str]] = None) -> list:
        """Validate and convert unsupported block types to text descriptions.

        This method filters content blocks based on the agent's supported modalities.
        Unsupported types are converted to informational text blocks describing
        what was received but cannot be processed.

        Args:
            file_blocks: List of content blocks to validate
            supported_modes: List of supported content types (e.g., ["text", "image", "file"]).
                           If None, defaults to all known types (permissive mode).
                           Common values: ["text"], ["text", "image"], ["text", "image", "audio", "video", "file"]

        Returns:
            Validated list with unsupported types converted to text descriptions or filtered out
        """
        # Default to all known types if not specified
        if supported_modes is None:
            supported_modes = ["text", "image", "audio", "video", "file"]
        else:
            # Ensure text is always included for metadata
            supported_modes = list(supported_modes) + ["text"]
            supported_modes = list(set(supported_modes))  # Remove duplicates

        validated_blocks = []

        for block in file_blocks:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type", "text")

            if block_type not in supported_modes:
                # Convert unsupported block to informative text, including URL/mime if present
                url = block.get("url")
                mime_type = block.get("mime_type")

                if url:
                    mime_str = f" ({mime_type})" if mime_type else ""
                    description = f"[{block_type.upper()}{mime_str}: {url}]"
                else:
                    description = f"[{block_type.upper()} content (not supported by this agent)]"

                text_block = {"type": "text", "text": description}
                validated_blocks.append(text_block)
                logger.debug(f"Converted unsupported block type '{block_type}' to text: {description}")
            else:
                validated_blocks.append(block)

        return validated_blocks


class LocalA2ARunnable(CostTrackingMixin, BaseA2ARunnable):
    """Base class for local (in-process) A2A sub-agents.

    Provides automatic checkpoint isolation and cost tracking for local sub-agents.

    This base class combines:
    1. Infrastructure layer: Automatic checkpoint isolation via abstract methods
    2. Observability layer: Automatic cost tracking (inherited from CostTrackingMixin)

    Subclasses must implement:
    - `name` property: Sub-agent identifier
    - `get_checkpoint_ns()`: Return checkpoint namespace
    - `get_sub_agent_identifier()`: Return identifier for cost tracking tags
    - `_process()` OR `_astream_impl()`: Processing logic (implement at least one)

    Optional overrides:
    - `get_thread_id()`: Custom thread_id pattern (default: {context_id}::{checkpoint_ns})
    - `get_checkpointer()`: Custom checkpointer backend (default: None = inherit parent)
    - Both `_process()` and `_astream_impl()` can be implemented for dual support

    Example:
        class MyLocalAgent(LocalA2ARunnable):
            @property
            def name(self) -> str:
                return "my-agent"

            def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
                return "my-agent"

            def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
                return "my-agent"

            async def _process(
                self,
                input_data: SubAgentInput,
                config: Dict[str, Any]
            ) -> Dict[str, Any]:
                content = self._extract_message_content(input_data)
                context_id, _ = self._extract_tracking_ids(input_data)
                result = await do_something(content)
                return self._build_success_response(result, context_id)
    """

    @abstractmethod
    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        """Get checkpoint namespace for this sub-agent.

        Used for checkpoint isolation. Should return a unique identifier
        for this sub-agent type (e.g., "task-scheduler", "general-purpose").

        Args:
            input_data: Validated input data

        Returns:
            Checkpoint namespace string
        """
        ...

    @abstractmethod
    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        """Get sub-agent identifier for cost tracking tags.

        Used to tag costs with the correct sub-agent. Should return the
        identifier to use in "sub_agent:{identifier}" tag.

        Args:
            input_data: Validated input data

        Returns:
            Sub-agent identifier string (e.g., "task-scheduler", "123" for dynamic agents)
        """
        ...

    def get_thread_id(self, context_id: str, input_data: SubAgentInput) -> str:
        """Build thread_id for checkpoint isolation.

        Default pattern: {context_id}::{checkpoint_ns}
        Override for custom thread_id patterns.

        Args:
            context_id: Conversation context ID
            input_data: Validated input data

        Returns:
            Thread ID string
        """
        checkpoint_ns = self.get_checkpoint_ns(input_data)
        return f"{context_id}::{checkpoint_ns}" if context_id else checkpoint_ns

    def get_checkpointer(self, input_data: SubAgentInput) -> Optional[Any]:
        """Get checkpointer override for custom backends.

        Default: None (inherit parent's checkpointer)
        Override for sub-agents that need a different checkpoint backend
        (e.g., dynamic agents with DynamoDB instead of PostgreSQL).

        Args:
            input_data: Validated input data

        Returns:
            Checkpointer instance or None
        """
        return None

    @abstractmethod
    def get_supported_input_modes(self) -> List[str]:
        """Get list of input modes (content types) supported by this sub-agent.

        Declares what types of content this agent can process. Used by:
        1. Orchestrator to determine what file types can be attached
        2. Agent itself to validate and filter incoming content blocks
        3. Deepagents middleware for content block preparation

        Should return a subset of supported types based on the agent's
        underlying model and configuration. Common values:
        - ["text"] - Text-only agent (default for non-multimodal models)
        - ["text", "image"] - Text + image support (most modern LLMs)
        - ["text", "image", "audio"] - Extended multimedia support

        Returns:
            List of supported content type strings (static, does not depend on request)
        """
        ...

    @property
    def input_modes(self) -> List[str]:
        """Return the list of input modalities supported by this agent.

        Concrete implementation that delegates to get_supported_input_modes().
        Provides backward compatibility and standard property interface.

        Returns:
            List of supported content types (e.g., ['text', 'image'])
        """
        return self.get_supported_input_modes()

    def get_model_type(self) -> Optional[str]:
        """Return the model type used by this agent, if known.

        Override in subclasses that know their model type to enable
        provider-specific content transformations in _prepare_human_message_input
        (e.g., Bedrock image→base64 conversion, Gemini MIME inference).

        Returns:
            ModelType string (e.g., "claude-sonnet-4.5") or None if unknown
        """
        return None

    async def _apply_provider_transforms(self, content_blocks: List) -> List:
        """Apply provider-specific content block transformations.

        Applies transformations based on get_model_type():
        - **Bedrock** (Claude): Converts URL-based images to inline base64
          (Bedrock Converse API rejects image URLs)
        - **Gemini** (Google): Infers missing MIME types from URL extensions
          (Gemini requires MIME types on file blocks)

        Args:
            content_blocks: Validated content blocks (text + file blocks)

        Returns:
            Transformed content blocks ready for the provider's API
        """
        model_type = self.get_model_type()
        if not model_type:
            return content_blocks

        try:
            backend = get_model_backend(model_type)
        except ValueError:
            return content_blocks

        if backend == "bedrock":
            return await self._transform_blocks_for_bedrock(content_blocks)
        elif backend == "google":
            return self._infer_mime_types(content_blocks)

        return content_blocks

    @staticmethod
    async def _transform_blocks_for_bedrock(content_blocks: List) -> List:
        """Convert URL-based images to inline base64 for Bedrock Converse API.

        Bedrock requires images as inline base64 data, not URLs. Delegates to
        the shared bedrock_image_processor utility.
        """

        return await preprocess_content_blocks_for_bedrock(content_blocks)

    @staticmethod
    def _infer_mime_types(content_blocks: List) -> List:
        """Infer missing MIME types from URL file extensions for Gemini.

        Gemini requires MIME types on file/audio/video blocks. When the
        orchestrator dispatches blocks without MIME types, infer from extension.

        NOTE: file-analyzer has a more sophisticated MIME inference mechanism,
              we could unify this in the future if needed.
        """
        _EXT_TO_MIME = {
            ".pdf": "application/pdf",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".webm": "audio/webm",
            ".m4a": "audio/m4a",
            ".flac": "audio/flac",
            ".mp4": "video/mp4",
            ".avi": "video/avi",
            ".mov": "video/quicktime",
        }
        result = []
        for block in content_blocks:
            if not isinstance(block, dict):
                result.append(block)
                continue
            block_type = block.get("type", "")
            url = block.get("url", "")
            if block_type in ("file", "audio", "video") and url and "mime_type" not in block:
                ext = ("." + url.split("?")[0].rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()) if "." in url else ""
                mime = _EXT_TO_MIME.get(ext)
                if mime:
                    block = {**block, "mime_type": mime}
                    logger.debug(f"Inferred MIME type '{mime}' from extension '{ext}' for Gemini")
            result.append(block)
        return result

    def extend_config_for_checkpoint_isolation(
        self,
        config: Dict[str, Any],
        thread_id: str,
        checkpoint_ns: str,
        checkpointer: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Extend parent config with checkpoint isolation parameters.

        Infrastructure layer: Handles checkpoint isolation for local sub-agents
        by inheriting parent config and overriding only checkpoint-specific settings.

        This ensures:
        - Metadata (user_id, assistant_id) is inherited from orchestrator
        - Tags and callbacks are inherited from parent
        - Checkpoint state is isolated via unique thread_id and checkpoint_ns

        Args:
            config: Parent config from orchestrator (contains metadata, tags, callbacks)
            thread_id: Unique thread_id for checkpoint isolation (e.g., "{context_id}::task-scheduler")
            checkpoint_ns: Checkpoint namespace for isolation (e.g., "task-scheduler")
            checkpointer: Optional checkpointer override (for dynamic agents needing different backend)

        Returns:
            New config dict with inherited parent config and checkpoint isolation overrides

        Example:
            config = self.extend_config_for_checkpoint_isolation(
                config=parent_config,
                thread_id=f"{context_id}::task-scheduler",
                checkpoint_ns="task-scheduler"
            )
        """
        extended = {
            **config,  # Inherit all parent config (metadata, tags, callbacks)
            "configurable": {
                **config.get("configurable", {}),
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            },
        }

        # Override checkpointer if provided (for dynamic agents)
        if checkpointer is not None:
            extended["configurable"]["__pregel_checkpointer"] = checkpointer

        return extended

    def extend_config_for_subagent(
        self,
        config: Dict[str, Any],
        sub_agent_identifier: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpointer: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Extend parent config with checkpoint isolation AND cost tracking.

        Unified method that handles both infrastructure and observability layers:
        1. Checkpoint isolation (thread_id, checkpoint_ns, checkpointer)
        2. Cost tracking tag extension (sub_agent:{identifier})

        This is the primary method used by ainvoke() to prepare config for sub-agents.

        Args:
            config: Parent config from orchestrator
            sub_agent_identifier: Identifier for cost tracking tag
            thread_id: Unique thread_id for checkpoint isolation
            checkpoint_ns: Checkpoint namespace for isolation
            checkpointer: Optional checkpointer override

        Returns:
            Extended config with checkpoint isolation and cost tracking
        """
        # 1. Infrastructure layer: checkpoint isolation
        extended = self.extend_config_for_checkpoint_isolation(
            config=config,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpointer=checkpointer,
        )

        # 2. Observability layer: cost tracking tags
        extended["tags"] = extended.get("tags", []) + [f"sub_agent:{sub_agent_identifier}"]

        return extended

    async def _extract_and_validate_blocks(
        self,
        input_data: SubAgentInput,
    ) -> tuple[str, list]:
        """Parse input into text content and validated file blocks.

        Extracts text and file blocks from agent input, validates block types
        against the agent's supported input modes, rejects S3 URIs, and
        converts unsupported block types to informational text.

        This is the composable building block for _prepare_human_message_input.
        Subclasses that need to post-process file blocks (e.g., MIME correction,
        file fetching) should call this directly instead of super()._prepare_...
        to avoid an unnecessary decompose/recompose roundtrip.

        Args:
            input_data: Validated sub-agent input with messages and content blocks

        Returns:
            Tuple of (text_content, validated_file_blocks)

        Raises:
            ValueError: If no messages or content provided, or if S3 URIs are present
        """
        if not input_data.messages:
            raise ValueError("No messages provided")

        raw_content = input_data.messages[-1].content
        if not raw_content:
            raise ValueError("No input content provided")

        # Parse content into text and file blocks
        if isinstance(raw_content, str):
            text_content = raw_content
            file_blocks = []
        else:
            text_parts: list[str] = []
            file_blocks: list = []

            for block in raw_content:
                if isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text_parts.append(block.get("text", ""))
                    elif block_type in ("image", "audio", "video", "file"):
                        file_blocks.append(block)

            text_content = "\n".join(text_parts) if text_parts else ""

        # Reject S3 URIs — agents cannot access them without presigning
        s3_blocks = [b for b in file_blocks if isinstance(b, dict) and b.get("url", "").startswith("s3://")]
        if s3_blocks:
            raise ValueError(
                f"Cannot directly access S3 URIs. Please provide a presigned URL for: {s3_blocks[0].get('url')}"
            )

        # Determine supported modes for this agent
        supported_modes = None
        if isinstance(self, LocalA2ARunnable):
            supported_modes = self.get_supported_input_modes()

        # Validate block types based on agent's supported modes
        validated_blocks = self._ensure_supported_block_types(file_blocks, supported_modes=supported_modes)

        return text_content, validated_blocks

    async def _prepare_human_message_input(
        self,
        input_data: SubAgentInput,
    ) -> HumanMessage:
        """Prepare a HumanMessage with multi-modal content blocks for LLM consumption.

        This is the unified entry point for content extraction and validation.
        It handles:
        1. Extracting text content and file blocks from the agent input
        2. Rejecting S3 URIs (require presigned HTTPS URLs)
        3. Validating block types against the agent's supported input modes
        4. Converting unsupported block types to informational text
        5. Provider-specific content transformations:
           - Bedrock: converts URL-based images to inline base64
           - Gemini: infers missing MIME types from URL extensions
        6. Building a HumanMessage ready for consumption by local LLMs

        For LocalA2ARunnable subclasses, this uses get_supported_input_modes() to determine
        which content types are valid. Blocks that don't match the agent's capabilities
        are converted to text descriptions.

        Subclasses needing to post-process file blocks (e.g., MIME correction,
        file fetching) should override this and call _extract_and_validate_blocks()
        directly to avoid decomposing the HumanMessage.

        Args:
            input_data: Validated sub-agent input with messages and content blocks

        Returns:
            HumanMessage ready for LLM consumption (with or without content_blocks)
            - If no files: simple text HumanMessage
            - If files: HumanMessage with content_blocks (text + filtered file blocks)
        """
        text_content, validated_blocks = await self._extract_and_validate_blocks(input_data)

        # Build HumanMessage with content blocks if any files present
        if validated_blocks:
            content_blocks: List[ContentBlock] = []

            if text_content:
                content_blocks.append({"type": "text", "text": text_content})

            content_blocks.extend(validated_blocks)

            # Apply provider-specific transformations
            if isinstance(self, LocalA2ARunnable):
                content_blocks = await self._apply_provider_transforms(content_blocks)

            return HumanMessage(content=content_blocks)  # type: ignore[arg-type]
        else:
            return HumanMessage(content=text_content)

    async def _process(self, input_data: SubAgentInput, config: Dict[str, Any]) -> TaskResponseData:
        """Non-streaming process implementation.

        Override for agents that don't support streaming.
        Used as fallback when _astream_impl is not implemented.
        At least one of _process or _astream_impl must be implemented.

        Args:
            input_data: Validated input with messages, a2a_tracking, and files
            config: Parent config from LangChain invocation context (for metadata propagation)

        Returns:
            TaskResponseData with messages and A2A metadata (plain, will be wrapped by astream)

        Example:
            async def _process(self, input_data: SubAgentInput, config: Optional[Dict[str, Any]] = None) -> TaskResponseData:
                content = self._extract_message_content(input_data)
                context_id, task_id = self._extract_tracking_ids(input_data)
                result = await do_something(content)
                return self._build_success_response(result, context_id=context_id)
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement either _astream_impl() or _process()")

    def _instrument(self, input_data: SubAgentInput, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Add instrumentation for cost tracking and observability.

        This method can be called at the start of streaming to set up any necessary
        instrumentation context (e.g., cost tracking tags, logging context).

        Args:
            input_data: Validated input with messages, a2a_tracking, and files
            config: Optional parent config from LangChain invocation context (for metadata propagation)
        """

        # Require parent config for proper metadata propagation
        if not config:
            raise ValueError(
                f"Local sub-agent '{self.name}' requires parent config from orchestrator. "
                "Missing config means incorrect user_id/assistant_id values would be used. "
                "This is a programming error - orchestrator must always pass config to sub-agents."
            )

        # Extract context_id for thread_id construction
        context_id, _ = self._extract_tracking_ids(input_data)
        if not context_id:
            raise ValueError(f"Missing context_id for sub-agent '{self.name}'")

        # Build checkpoint isolation parameters via abstract methods
        thread_id = self.get_thread_id(context_id, input_data)
        checkpoint_ns = self.get_checkpoint_ns(input_data)
        checkpointer = self.get_checkpointer(input_data)
        sub_agent_id = self.get_sub_agent_identifier(input_data)

        # Extend config with checkpoint isolation + cost tracking
        extended_config = self.extend_config_for_subagent(
            config=config,
            sub_agent_identifier=sub_agent_id,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpointer=checkpointer,
        )
        return extended_config

    async def astream(
        self, input_data: Dict[str, Any], config: Optional[Dict[str, Any]] = None
    ) -> AsyncIterable[StreamEvent]:
        """Stream local sub-agent execution with real-time status updates.

        Enables streaming of intermediate progress from LangGraph-based sub-agents.
        For sub-agents that support streaming, this provides:
        - Real-time working-state status messages
        - Progress visibility during long-running operations
        - Terminal state with wrapped A2A metadata

        Automatically handles:
        1. Input validation
        2. Checkpoint isolation setup (via abstract methods)
        3. Cost tracking tag injection
        4. Config extension and propagation
        5. Response wrapping with A2A metadata

        Args:
            input_data: Input data matching SubAgentInput schema
            config: Parent config from orchestrator (contains metadata, tags, callbacks)

        Yields:
            Status update dictionaries compatible with A2AClientRunnable.astream format:
            - {"type": "task_update", "state": "working", "data": {...}, "is_complete": False}
            - {"type": "task_update", "state": "completed", "data": {...}, "is_complete": True}

        Raises:
            NotImplementedError: If subclass doesn't implement _astream_impl()
        """
        try:
            # Validate input
            validated = SubAgentInput.model_validate(input_data)

            # Instrumentation: set up cost tracking context
            extended_config = self._instrument(validated, config)
            logger.debug(
                f"[{self.name}] Streaming with config: thread_id={extended_config.get('configurable', {}).get('thread_id', '')}, "
                f"checkpoint_ns={extended_config.get('configurable', {}).get('checkpoint_ns', '')}, tags={extended_config.get('tags', [])}"
            )

            # Try streaming first
            try:
                async for item in self._astream_impl(validated, extended_config):
                    yield item
                return  # Streaming succeeded
            except NotImplementedError:
                pass  # Fall through to _process

            # Fallback for non-streaming agents: use _process directly
            # (Not ainvoke, which would create a circular dependency since ainvoke collects astream)
            logger.debug(f"[{self.name}] Streaming not implemented, falling back to _process")
            result = await self._process(validated, extended_config)
            wrapped = self._wrap_message_with_metadata(result)
            yield TaskUpdate(data=wrapped)
        except ValueError as e:
            # Content extraction errors
            logger.error(f"[{self.name}] Stream validation error: {e}")
            result = self._build_error_response(str(e))
            wrapped = self._wrap_message_with_metadata(result)
            yield ErrorEvent(error=str(e), data=wrapped)
        except Exception as e:
            logger.exception(f"Error streaming {self.name}: {e}")
            result = self._build_error_response(f"Internal error: {str(e)}")
            wrapped = self._wrap_message_with_metadata(result)
            yield ErrorEvent(error=str(e), data=wrapped)

    async def _astream_impl(self, input_data: SubAgentInput, config: Dict[str, Any]) -> AsyncIterable[StreamEvent]:
        """Stream implementation to be provided by subclasses.

        For LangGraph-based agents, this should:
        1. Stream the internal graph
        2. Extract working-state messages from intermediate events
        3. Yield status updates in A2A-compatible format
        4. Return terminal result

        Args:
            input_data: Validated input with messages and tracking IDs
            config: Extended config with checkpoint isolation and cost tracking

        Yields:
            Dict with format matching A2AClientRunnable.astream:
            - {"type": "task_update", "state": "working", "data": {...}, "is_complete": False}
            - {"type": "task_update", "state": "completed", "data": {...}, "is_complete": True}

        Raises:
            NotImplementedError: Default implementation for non-streaming agents
        """
        raise NotImplementedError(f"{self.__class__.__name__} doesn't implement streaming")
        yield  # Make this an async generator so callers get an AsyncIterator, not a coroutine
