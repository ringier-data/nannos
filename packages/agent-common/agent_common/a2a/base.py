"""Base classes for A2A Runnable implementations.

Provides the abstract base class and shared utilities for both remote (A2A over
HTTP) and local (in-process) sub-agents.

Design Principles:
1. Every sub-agent is reached through the A2A task lifecycle. A remote one over
   HTTP; a local one through the in-process server in
   ``agent_common.a2a.local_server`` — same protocol, no network hop. The caller
   sees one stream of typed ``StreamEvent``s either way.
2. A result is a typed ``TaskResponseData`` (task id, context id, state,
   metadata) — never a JSON envelope inside the message text.
3. Abstract interface allows type-safe usage across the codebase
"""

import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from a2a.types import Task, TaskState
from langchain_core.messages import AIMessage, ContentBlock, HumanMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from pydantic import BaseModel, Field
from ringier_a2a_sdk.agent.cost_tracking_mixin import CostTrackingMixin
from ringier_a2a_sdk.utils.bedrock_image_processor import preprocess_blocks_for_chat_completions

from .event_translation import A2AStreamTranslator
from .message_conversion import PROPOSED_TASK_ID_KEY, human_messages_to_a2a_message
from .stream_events import ErrorEvent, StreamEvent, TaskResponseData, TaskUpdate

if TYPE_CHECKING:
    from .local_server import LocalA2AServer

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
    message_formatting: Optional[str] = Field(
        default=None,
        description=(
            "How the channel that delivers the answer renders text ('slack', 'google-chat', "
            "'plain', 'markdown'). Rides the outgoing A2A message metadata as "
            "`messageFormatting`, which is the same key an interactive client sends, so a "
            "remote agent applies its own formatting rules without this side touching its prompt. "
            "SET IT ONLY when the invoked agent's own text is what reaches the user — a "
            "scheduled job dispatched by agent-runner, where the answer goes straight to a "
            "delivery channel. Leave it None whenever an orchestrator is routing: the "
            "orchestrator composes the delivered message and applies the channel's rules to "
            "it, so the sub-agent writes raw material for that and rules about a medium it "
            "never writes to would only spend its prompt."
        ),
    )
    proposed_task_id: Optional[str] = Field(
        default=None,
        description=(
            "The id the caller wants the task this message OPENS to have. A2A servers mint task ids; "
            "the orchestrator names a delegation's task after the tool call that made it, so a LangGraph "
            "replay of that call finds the task it already opened instead of running the work twice. "
            "Ignored when the message continues an existing task (a2a_tracking carries a live task_id), "
            "and by servers that do not honour the proposal (remote agents)."
        ),
    )


class BaseA2ARunnable(ABC):
    """Abstract base class for A2A Runnables.

    Defines the common interface and shared utilities for both remote (A2A over
    HTTP) and local (in-process) sub-agents.

    Every sub-agent streams the same typed events (``TaskUpdate`` /
    ``ArtifactUpdate`` / ``ErrorEvent``); a ``TaskUpdate`` carries a
    ``TaskResponseData`` whose ``task_id`` / ``context_id`` / ``state`` are the
    A2A task's own, and whose ``messages[-1].content`` is the plain text the
    agent produced. The exchange ends on a terminal state or on an intervention
    state (``input_required`` / ``auth_required``), which the caller answers by
    sending the next message to the same ``task_id``.
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

    @property
    def tracking_key(self) -> str:
        """The key this runnable's tracking state lives under in a2a_tracking.

        Single home for the convention: anything seeding or reading
        a2a_tracking entries for this runnable (the orchestrator's dispatch
        middleware, conversation adoption, _extract_tracking_ids) must use
        this key, not re-derive it from the name.
        """
        return self.name.replace(" ", "")

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

    def _build_response(
        self,
        content: str,
        *,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
        state: TaskState = TaskState.TASK_STATE_COMPLETED,
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
            state=TaskState.TASK_STATE_FAILED,
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
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
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
            state=TaskState.TASK_STATE_COMPLETED,
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
        agent_name = self.tracking_key
        agent_tracking = input_data.a2a_tracking.get(agent_name, {})

        # Waterfall: Try persisted context_id first, fallback to orchestrator's
        context_id = agent_tracking.get("context_id") if agent_tracking else None

        # Pre-ADR-0008 adoption records name the run's conversation under
        # ``adopt_thread_from``: the dispatch used to FORK that checkpoint into
        # the conversation's own thread. There is no fork any more — the run's
        # context id IS the thread (``local_sub_agent_thread_id``) — so the old
        # key means exactly what ``context_id`` means now. Reading it keeps
        # conversations that adopted a run before the deploy pointed at the run's
        # conversation instead of silently starting blank.
        if not context_id and agent_tracking:
            legacy_context_id = agent_tracking.get("adopt_thread_from")
            if legacy_context_id:
                context_id = legacy_context_id
                logger.warning(
                    f"[CONVERSATION_ID] '{agent_name}' carries a pre-ADR-0008 adoption record "
                    f"(adopt_thread_from={context_id}); continuing the run's conversation under it. "
                    f"``a2a_tracking`` records the same id as context_id once this delegation returns, "
                    f"so the legacy key is read at most once per conversation."
                )

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

    def get_sub_agent_config_version_id(self, input_data: SubAgentInput) -> Optional[int]:
        """Return the running config-version id for cost attribution, or None.

        When known, it is added as a ``sub_agent_config_version:{id}`` tag so the
        gateway spend log is attributed to the exact config version rather than the
        agent's default version (which the backend would otherwise infer from
        sub_agent_id). Defaults to None; sub-agents that know their version override.
        """
        return None

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
        """Normalize content blocks the LLM client cannot serialize.

        The app no longer knows the target provider — every model is reached through the
        LiteLLM gateway, which normalizes content for it (image fetch/encode for Bedrock,
        MIME inference for Gemini). So provider-specific handling is the gateway's job,
        with the exception of what the gateway never gets to see:

        Because the gateway speaks the OpenAI protocol, every call is built by
        ``langchain_openai`` against the Chat Completions spec, and ``langchain_core``'s
        block translator *raises* on three block shapes while assembling the payload —
        client-side, before a request exists, so no gateway-side normalization can help.
        A ``file`` or ``audio`` block carrying a ``url`` ("OpenAI Chat Completions does
        not support file URLs" / "Key base64 is required for audio blocks") is inlined as
        base64 here; a ``video`` block, which the spec cannot express at all, degrades to
        a text description. Image URLs are unaffected: they map to ``image_url``.

        Args:
            content_blocks: Validated content blocks (text + file blocks)

        Returns:
            Content blocks the OpenAI-compatible client can serialize
        """
        return await preprocess_blocks_for_chat_completions(content_blocks)

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
        sub_agent_config_version_id: Optional[int] = None,
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
        tags = extended.get("tags", []) + [f"sub_agent:{sub_agent_identifier}"]
        if sub_agent_config_version_id is not None:
            tags.append(f"sub_agent_config_version:{sub_agent_config_version_id}")
        extended["tags"] = tags

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
        5. Normalizing blocks the LLM client cannot serialize (URL-sourced file blocks
           are inlined as base64 — see _apply_provider_transforms)
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
            sub_agent_config_version_id=self.get_sub_agent_config_version_id(input_data),
        )
        return extended_config

    # ------------------------------------------------------------------
    # The A2A side: a local sub-agent is served in-process
    # ------------------------------------------------------------------

    @property
    def local_server(self) -> "LocalA2AServer":
        """The in-process A2A server this runnable is reached through.

        Built lazily, once per runnable, on the process-wide task store
        (``local_server.get_local_task_store``). Everything the orchestrator
        does with a remote agent — open a task, read it back, answer a pause,
        cancel it — it does with a local one through this object.
        """
        server = getattr(self, "_local_server", None)
        if server is None:
            from .local_server import LocalA2AServer

            server = LocalA2AServer(self)
            self._local_server = server
        return server

    async def aget_task(self, task_id: str) -> Optional[Task]:
        """``tasks/get`` for one of this agent's tasks; ``None`` for an unknown id."""
        return await self.local_server.get_task(task_id)

    async def cancel_task(self, task_id: str) -> None:
        """``tasks/cancel`` for one of this agent's tasks. Best-effort, never raises."""
        try:
            await self.local_server.cancel(task_id)
        except Exception:
            logger.warning(f"Failed to cancel local task {task_id} on {self.name}", exc_info=True)

    async def aget_pending_interrupts(self, config: Dict[str, Any]) -> list:
        """The LangGraph interrupts the agent's thread is currently parked on.

        Read by the in-process executor before each message: a non-empty list
        means the message is the ANSWER to those interrupts and is delivered as
        ``Command(resume)``; an empty list means it is new input. The default is
        "never parked", right for agents with no checkpointed graph; graph-backed
        agents override it with ``graph.aget_state(config).interrupts``.

        Args:
            config: The instrumented run config (``_instrument``) naming the
                thread to inspect.
        """
        return []

    async def astream(
        self, input_data: Dict[str, Any] | Any, config: Optional[Dict[str, Any]] = None
    ) -> AsyncIterable[StreamEvent]:
        """Stream one A2A exchange with this local sub-agent.

        The caller's side of the task lifecycle: ``input_data`` is turned into an
        A2A message (continuing the task ``a2a_tracking`` names, or opening a new
        one under ``proposed_task_id``), sent to the in-process server, and the
        server's events come back as the same typed ``StreamEvent``s a remote
        agent's ``astream`` yields. HITL is not special here — a paused task ends
        the exchange on ``input_required`` / ``auth_required`` with the interrupts
        in ``data.metadata["interrupts"]``, and the answer is the next ``astream``
        call addressed to that task.

        Args:
            input_data: ``SubAgentInput``-shaped input. A LangGraph ``Command`` is
                not accepted: resumes travel as messages (``answer_to_human_message``).
            config: Parent ``RunnableConfig`` from the caller. Required — the
                graph inherits its callbacks, tags, metadata and checkpointer.
        """
        try:
            if isinstance(input_data, Command):
                raise ValueError(
                    f"Local sub-agent '{self.name}' is resumed by sending the answer as a message to the "
                    "paused task, not with a Command (see agent_common.a2a.message_conversion)."
                )
            if not config:
                raise ValueError(
                    f"Local sub-agent '{self.name}' requires parent config from orchestrator. "
                    "Missing config means incorrect user_id/assistant_id values would be used. "
                    "This is a programming error - orchestrator must always pass config to sub-agents."
                )
            validated = SubAgentInput.model_validate(input_data)
            context_id, task_id = self._extract_tracking_ids(validated)
            if not context_id:
                raise ValueError(f"Missing context_id for sub-agent '{self.name}'")
            extra_metadata = None
            if not task_id and validated.proposed_task_id:
                extra_metadata = {PROPOSED_TASK_ID_KEY: validated.proposed_task_id}
            message = human_messages_to_a2a_message(
                validated.messages,
                context_id,
                task_id,
                scheduled_job_id=validated.scheduled_job_id,
                message_formatting=validated.message_formatting,
                extra_metadata=extra_metadata,
            )
            logger.debug(
                f"[{self.name}] A2A message {message.message_id} -> in-process server "
                f"(context_id={context_id}, task_id={task_id or '(new)'})"
            )
            translator = A2AStreamTranslator()
            # ``aclosing``: a caller that stops iterating early must close the
            # server's stream now, not when the abandoned generator chain is
            # garbage-collected — that is where the SDK unsubscribes and the
            # task's live object is released (ADR-0008, "The live object is a cache").
            async with aclosing(self.local_server.send(message, parent_config=config)) as events:
                async for event in events:
                    for out in translator.translate(event):
                        yield out
            if translator.task_updates == 0:
                yield ErrorEvent(error=f"No response received from sub-agent '{self.name}'")
        except GraphInterrupt:
            # Never expected past the server, but if a graph interrupt ever escapes
            # it belongs to the caller's Pregel, not to an error message.
            raise
        except ValueError as e:
            logger.error(f"[{self.name}] Stream validation error: {e}")
            yield ErrorEvent(error=str(e), data=self._build_error_response(str(e)))
        except Exception as e:
            logger.exception(f"Error streaming {self.name}: {e}")
            yield ErrorEvent(error=str(e), data=self._build_error_response(f"Internal error: {str(e)}"))

    # ------------------------------------------------------------------
    # The graph side: what the in-process server runs
    # ------------------------------------------------------------------

    async def astream_graph(
        self, input_data: Dict[str, Any] | Any, config: Optional[Dict[str, Any]] = None
    ) -> AsyncIterable[StreamEvent]:
        """Run this agent's graph directly and stream its events.

        This is the server side of ``astream``: the in-process executor calls it
        with the message's ``SubAgentInput`` (already instrumented config) or with
        the ``Command(resume)`` it built for a paused thread, and translates what
        comes out into A2A events. It is also what a host that IS the A2A server
        for this agent already (the orchestrator's embedded execute-only path)
        drives directly.

        Handles input validation, checkpoint isolation and cost-tracking setup
        (``_instrument``) for a fresh ``SubAgentInput`` — a ``Command`` skips both
        and is fed to the graph as-is — then delegates to ``_astream_impl``, or to
        ``_process`` for agents that do not stream.

        A ``GraphInterrupt`` (the graph parked on a question) propagates: the
        executor turns it into the task's ``input_required`` / ``auth_required``
        state.
        """
        try:
            if isinstance(input_data, Command):
                logger.info(f"[{self.name}] Streaming with Command (resume)")
                async for item in self._astream_impl(input_data, config or {}):
                    yield item
                return

            validated = SubAgentInput.model_validate(input_data)
            extended_config = self._instrument(validated, config)
            logger.debug(
                f"[{self.name}] Streaming with config: thread_id={extended_config.get('configurable', {}).get('thread_id', '')}, "
                f"checkpoint_ns={extended_config.get('configurable', {}).get('checkpoint_ns', '')}, tags={extended_config.get('tags', [])}"
            )

            try:
                async for item in self._astream_impl(validated, extended_config):
                    yield item
                return
            except NotImplementedError:
                pass

            logger.debug(f"[{self.name}] Streaming not implemented, falling back to _process")
            result = await self._process(validated, extended_config)
            yield TaskUpdate(data=result)
        except GraphInterrupt:
            # A LangGraph interrupt signal (e.g. from HumanInTheLoopMiddleware). It
            # must NOT be swallowed — the executor surfaces it as the task's pause.
            raise
        except ValueError as e:
            logger.error(f"[{self.name}] Stream validation error: {e}")
            yield ErrorEvent(error=str(e), data=self._build_error_response(str(e)))
        except Exception as e:
            logger.exception(f"Error streaming {self.name}: {e}")
            yield ErrorEvent(error=str(e), data=self._build_error_response(f"Internal error: {str(e)}"))

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
