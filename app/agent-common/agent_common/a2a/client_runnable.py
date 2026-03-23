"""
A2A Runnable implementation for remote A2A agents.

Provides streaming and non-streaming interfaces for Agent-to-Agent (A2A) communication,
making it compatible with LangChain/DeepAgents while enabling real-time status updates.

This module contains:
- A2AClientRunnable: Remote A2A agent client (extends BaseA2ARunnable)
- SubAgentInput: Re-exported from base for backwards compatibility

For local sub-agents, see LocalA2ARunnable in base.py.

TODO: this module could be better aligned with A2A SDK types and patterns.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterable, Sequence
from typing import Any, Dict, List, Optional

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    FilePart,
    FileWithUri,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TextPart,
    TransportProtocol,
)
from a2a.types import (
    Part as A2APart,
)
from a2a.types import (
    Role as A2ARole,
)
from langchain_core.messages import AIMessage, ContentBlock
from langsmith.run_helpers import get_current_run_tree

from agent_common.a2a.authentication import (
    AuthenticationMethod,
    AuthPayload,
    ServiceAuthRequirement,
)
from agent_common.a2a.base import BaseA2ARunnable, SubAgentInput
from agent_common.a2a.config import A2AClientConfig
from agent_common.a2a.stream_events import (
    TERMINAL_STATES,
    ArtifactUpdate,
    ErrorEvent,
    StreamEvent,
    TaskResponseData,
    TaskUpdate,
    parse_event_metadata,
)

logger = logging.getLogger(__name__)


class A2AClientRunnable(BaseA2ARunnable):
    """A2A Runnable with streaming and non-streaming interfaces.

    Provides both streaming (astream) and non-streaming (ainvoke) interfaces,
    making it compatible with LangChain/DeepAgents while enabling real-time
    status updates via the A2A protocol.
    """

    def __init__(
        self,
        agent_card: AgentCard,
        config: Optional[A2AClientConfig] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        """Initialize the A2A runnable.

        Args:
            agent_card: AgentCard for the target A2A agent
            config: Optional A2A client configuration
            http_client: Optional HTTP client (will be created if not provided)
        """
        self.agent_card = agent_card
        self.config = config or A2AClientConfig()
        self._http_client = http_client
        self._close_http_client = http_client is None
        self._client: Optional[Client] = None

    @property
    def name(self) -> str:
        """Return the agent name (used for tracking)."""
        return self.agent_card.name

    @property
    def input_modes(self) -> List[str]:
        return self.agent_card.default_input_modes or []

    @property
    def description(self) -> str:
        """Return the agent description."""
        return self.agent_card.description or "No description provided."

    async def _inject_trace_headers(self, request: httpx.Request) -> None:
        """Inject LangSmith distributed tracing headers into each request.

        This event hook is called for every HTTP request, allowing us to
        dynamically inject trace context headers based on the current run.
        """
        if run_tree := get_current_run_tree():
            trace_headers = run_tree.to_headers()
            request.headers.update(trace_headers)
            logger.debug(f"Injected LangSmith trace headers: {list(trace_headers.keys())}")

    async def _get_client(self) -> Client:
        """Lazy initialization of A2A client."""
        if self._client is None:
            if self._http_client is None:
                timeout = httpx.Timeout(
                    connect=self.config.timeout_connect,
                    read=self.config.timeout_read,
                    write=self.config.timeout_write,
                    pool=self.config.timeout_pool,
                )
                headers = {"User-Agent": f"{self.config.user_agent_prefix} (A2A-Client)"}

                # Create httpx client with event hook for dynamic trace header injection
                self._http_client = httpx.AsyncClient(
                    timeout=timeout, headers=headers, event_hooks={"request": [self._inject_trace_headers]}
                )

            client_config = ClientConfig(
                httpx_client=self._http_client,
                supported_transports=[TransportProtocol.jsonrpc],
            )
            factory = ClientFactory(client_config)
            interceptors = [self.config.auth_interceptor] if self.config.auth_interceptor else None
            self._client = factory.create(self.agent_card, interceptors=interceptors)  # type: ignore

        return self._client

    def _extract_text_from_parts(self, parts: Sequence[A2APart]) -> str:
        """Extract text content from A2A parts."""
        # TODO: what to do with files?
        texts = []
        for part in parts:
            inner_part = part.root
            if inner_part.kind == "text":
                texts.append(inner_part.text)
            elif inner_part.kind == "data":
                texts.append(json.dumps(inner_part.data))
        return "\n".join(texts) if texts else ""

    def _parse_auth_payload(self, task_status) -> Dict[str, Any]:
        """Parse authentication payload from task status following CIBA patterns."""
        message_text = "Authentication required for downstream service"
        service_name = "unknown_service"  # TODO: the application should provide this
        auth_methods = []

        # Extract information from task status message
        if task_status.message and task_status.message.parts:
            message_text = self._extract_text_from_parts(task_status.message.parts)

            # Try to parse structured auth info from message metadata
            try:
                for part in task_status.message.parts:
                    if part.root.kind == "data" and isinstance(part.root.data, dict):
                        logger.info(f"Parsing structured auth data: {part.root.data}")
                        auth_data = part.root.data
                        service_name = auth_data.get("service", "unknown_service")

                        # Extract authentication methods
                        if "auth_methods" in auth_data:
                            for method_data in auth_data["auth_methods"]:
                                auth_method = AuthenticationMethod(**method_data)
                                auth_methods.append(auth_method.model_dump())

                        # If no structured methods, use generic OAuth2 authentication
                        if not auth_methods:
                            auth_methods.append(
                                {
                                    "method": "oauth2",
                                    "description": "OAuth2 authentication required",
                                    "instructions": "Please complete the authentication flow",
                                }
                            )
                        break
            except Exception as e:
                logger.warning(f"Failed to parse structured auth data: {e}")

        # Create enterprise authentication payload
        # Use generic read scope as default
        default_scopes = ["read"]

        service_auth_requirement = ServiceAuthRequirement(
            service=service_name,
            auth_methods=[AuthenticationMethod(**method) for method in auth_methods]
            if auth_methods
            else [
                AuthenticationMethod(method="oauth2", description="Authentication required", instructions=message_text)
            ],
            required_scopes=default_scopes,
        )

        auth_payload = AuthPayload(
            requires_auth=True,
            auth_requirement=service_auth_requirement,
            correlation_id=task_status.message.message_id if task_status.message else None,
        )

        return {
            "auth_info": auth_payload.model_dump(),
            "auth_methods": [method.model_dump() for method in service_auth_requirement.auth_methods],
            "required_scopes": service_auth_requirement.required_scopes,
            "service": service_name,
            "instructions": message_text,
            "ciba_supported": any(method.method == "ciba" for method in service_auth_requirement.auth_methods),
            "device_code_supported": any(
                method.method == "device_code" for method in service_auth_requirement.auth_methods
            ),
            "corporate_sso_preferred": True,  # Indicate preference for corporate SSO
        }

    async def _handle_task_response(self, task: Task) -> TaskResponseData:
        """
        Process task response using clean A2A protocol compliance.

        Uses A2A SDK types directly and follows protocol specification.
        Returns TaskResponseData for type-safe downstream consumption.
        """
        # Application-specific metadata (separate from A2A protocol)
        app_metadata: dict[str, Any] = {}

        # Handle different task states following A2A protocol
        if task.status.state == TaskState.auth_required:
            auth_payload = self._parse_auth_payload(task.status)
            app_metadata.update(auth_payload)

        # Create synthetic message for LangChain compatibility
        content = self._create_synthetic_message_content(task, app_metadata)
        messages = [AIMessage(content=content)]
        logger.debug(f"Added synthetic message: {content}")

        return TaskResponseData(
            task_id=task.id,
            context_id=task.context_id,
            state=task.status.state,
            messages=messages,
            metadata=app_metadata if app_metadata else {},
        )

    @staticmethod
    def _extract_parts(parts: Sequence[A2APart]) -> list[Dict[str, Any]]:
        """Extract A2A parts into plain dicts.

        Works with both ``Message.parts`` and ``Artifact.parts`` since
        both are ``list[Part]`` (TextPart | FilePart | DataPart).
        """
        parts_data: list[Dict[str, Any]] = []
        for part in parts:
            inner = part.root
            if inner.kind == "text":
                parts_data.append({"type": "text", "content": inner.text, "metadata": inner.metadata or {}})
            elif inner.kind == "file":
                parts_data.append({"type": "file", "file": inner.file, "metadata": inner.metadata or {}})
            elif inner.kind == "data":
                parts_data.append({"type": "data", "content": inner.data, "metadata": inner.metadata or {}})
        return parts_data

    def _extract_artifacts_data(self, task: Task) -> list[Dict[str, Any]]:
        """Extract artifacts data following A2A protocol structure."""
        if not task.artifacts:
            return []
        return [
            {
                "id": artifact.artifact_id,
                "name": artifact.name,
                "description": artifact.description,
                "parts": self._extract_parts(artifact.parts),
            }
            for artifact in task.artifacts
        ]

    def _create_synthetic_message_content(self, task: Task, app_metadata: Dict[str, Any]) -> str:
        """Create synthetic message content following A2A protocol.

        For failed/incomplete tasks, explicitly indicates the state to ensure
        the LLM recognizes when a task did not complete successfully.

        DEFENSIVE DESIGN: Treats non-terminal states (working, input_required, etc.)
        as incomplete to prevent the orchestrator from incorrectly assuming success.
        """
        content = "Task processed"

        # A2A protocol: task.status.message contains human-readable details
        if task.status.message and task.status.message.parts:
            content = self._extract_text_from_parts(task.status.message.parts)
        elif task.status.state == TaskState.auth_required:
            content = app_metadata.get("instructions", "Authentication required")
        elif task.artifacts:
            # Use first artifact content if available (A2A protocol)
            first_artifact = task.artifacts[0]
            if first_artifact.parts:
                first_part = first_artifact.parts[0]
                inner_part = first_part.root
                if inner_part.kind == "text":
                    content = inner_part.text or "Task completed"

        # CRITICAL: For non-successful states, explicitly indicate the issue
        # This ensures the LLM understands the task did not complete successfully
        if task.status.state == TaskState.failed:
            # If content doesn't already indicate failure, prepend error marker
            lower_content = content.lower()
            if "failed" not in lower_content and "error" not in lower_content:
                content = f"ERROR: Task failed - {content}"
            else:
                # Content already mentions failure, but ensure it's prominent
                content = f"Task execution failed: {content}"
        elif task.status.state == TaskState.working:
            # Agent is still processing - this should NOT be treated as completion
            content = f"INCOMPLETE: Agent is still working - {content}"
        elif task.status.state not in TERMINAL_STATES:
            # Any other non-terminal state (input_required, auth_required, etc.)
            # Prepend state information for clarity
            state_name = task.status.state.value if hasattr(task.status.state, "value") else str(task.status.state)
            content = f"Agent status: {state_name} - {content}"

        return content

    async def _handle_message_response(self, message: Message) -> TaskResponseData:
        """Convert an A2A Message response into a TaskResponseData.

        Extracts text from message parts and wraps it as an AIMessage so
        that downstream consumers (which expect ``TaskResponseData.messages``)
        can process it uniformly — no separate ``MessageResponseData`` needed.
        """
        text = self._extract_text_from_parts(message.parts)
        return TaskResponseData(
            task_id=message.task_id or "",
            context_id=message.context_id or "",
            messages=[AIMessage(content=text)] if text else [],
            metadata={
                "message_id": message.message_id,
                "role": str(message.role),
                "parts": self._extract_parts(message.parts),
            },
        )

    async def ainvoke(self, input_data: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> StreamEvent:
        """Non-streaming invoke by collecting all stream events.

        Returns the final StreamEvent from the stream. If the stream ends
        without reaching a terminal state (completed, failed, canceled, rejected),
        returns an ErrorEvent indicating unexpected disconnect.

        Note: May log SSE cleanup warnings ("generator didn't stop after athrow()") which are
        cosmetic and don't affect functionality. These occur when asyncio.run() tears down the
        event loop before the A2A library's SSE connection fully cleans up.
        """
        last_event: StreamEvent | None = None

        try:
            async for item in self.astream(input_data, config):
                last_event = item
                # For errors, return immediately
                if isinstance(item, ErrorEvent):
                    return item

            if last_event is None:
                return ErrorEvent(error="No response received from agent")

            # CRITICAL: Check if stream ended without reaching a terminal state
            # This handles cases where sub-agent crashes or disconnects unexpectedly
            if isinstance(last_event, TaskUpdate):
                state = last_event.data.state

                if state not in TERMINAL_STATES and state not in (TaskState.input_required, TaskState.auth_required):
                    logger.warning(
                        f"A2A stream ended with non-terminal state: {state}. "
                        "Sub-agent may have crashed or disconnected. Treating as failure."
                    )
                    original_content = ""
                    if isinstance(last_event.data, TaskResponseData) and last_event.data.messages:
                        last_msg = last_event.data.messages[-1]
                        if hasattr(last_msg, "content"):
                            original_content = last_msg.content

                    return ErrorEvent(
                        error=f"The agent stopped responding unexpectedly. Last status: {state}. {original_content}",
                        data=last_event.data,
                    )

            return last_event

        except httpx.ConnectError as e:
            logger.error(f"A2A connection failed: {e}")
            return ErrorEvent(
                error="Unable to connect to A2A service. The service may be offline.",
                error_type=type(e).__name__,
                requires_retry=False,
            )
        except httpx.TimeoutException as e:
            logger.error(f"A2A request timed out: {e}")
            return ErrorEvent(
                error="A2A request timed out. The service may be slow or unavailable.",
                error_type=type(e).__name__,
                requires_retry=True,
            )
        except Exception as e:
            logger.error(f"A2A invocation failed: {e}")
            return ErrorEvent(
                error=str(e),
                error_type=type(e).__name__,
                requires_retry=True,
            )

    def stream(self, input_data: Dict[str, Any]):
        """Synchronous streaming is not supported - use astream instead.

        Raises:
            NotImplementedError: Always, as sync streaming is not supported
        """
        raise NotImplementedError("Synchronous streaming not supported for A2A. Use astream() instead.")

    @staticmethod
    def _extract_content_and_file_blocks(
        input_data: SubAgentInput,
    ) -> tuple[str, list[ContentBlock]]:
        """Extract text content and file ContentBlocks from the last message.

        When the dispatch middleware builds a HumanMessage with content_blocks
        (for file passthrough), `message.content` is a list of typed dicts.
        This method splits them into a text string and file blocks.

        Args:
            input_data: Validated sub-agent input

        Returns:
            Tuple of (text_content, file_blocks)
        """
        if not input_data.messages:
            raise ValueError("No messages provided")

        raw_content = input_data.messages[-1].content
        if not raw_content:
            raise ValueError("No input content provided")

        if isinstance(raw_content, str):
            return raw_content, []

        # content_blocks present — content is a list of dicts
        text_parts: list[str] = []
        file_blocks: list[ContentBlock] = []
        for block in raw_content:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type in ("image", "audio", "video", "file"):
                    file_blocks.append(block)  # type: ignore[arg-type]

        text = "\n".join(text_parts) if text_parts else ""
        return text, file_blocks

    @staticmethod
    def _content_block_to_file_part(block: ContentBlock) -> A2APart | None:
        """Convert a LangChain ContentBlock to an A2A FilePart.

        Maps ImageContentBlock, AudioContentBlock, VideoContentBlock, and
        FileContentBlock back to A2A FilePart(file=FileWithUri(...)).

        Args:
            block: Typed ContentBlock dict with 'url' and optionally 'mime_type'

        Returns:
            A2APart wrapping a FilePart, or None if no URL is present
        """
        if not isinstance(block, dict):
            return None
        url = block.get("url")
        if not url:
            return None
        mime_type = block.get("mime_type")
        file_with_uri = FileWithUri(uri=url, mime_type=mime_type)
        return A2APart(root=FilePart(file=file_with_uri))

    def _create_a2a_message(
        self,
        content: str,
        context_id: Optional[str],
        task_id: Optional[str],
        file_blocks: Optional[list[ContentBlock]] = None,
        scheduled_job_id: Optional[int] = None,
    ) -> Message:
        """Create an A2A message with proper metadata.

        Args:
            content: Text message content
            context_id: Optional context ID for multi-turn conversations
            task_id: Optional task ID for continuing existing tasks
            file_blocks: Optional ContentBlocks to forward as A2A FileParts
            scheduled_job_id: Optional scheduled job ID for cost attribution

        Returns:
            Constructed A2A Message
        """
        message_metadata: Dict[str, Any] = {
            "source": "Orchestrator",
            "timestamp": time.time(),
        }
        if scheduled_job_id is not None:
            message_metadata["scheduled_job_id"] = scheduled_job_id

        parts: list[A2APart] = [A2APart(root=TextPart(text=content, metadata=message_metadata))]

        # Convert ContentBlocks to A2A FileParts for deterministic file forwarding
        if file_blocks:
            for block in file_blocks:
                file_part = self._content_block_to_file_part(block)
                if file_part:
                    parts.append(file_part)
            logger.debug(f"A2A message includes {len(parts) - 1} FilePart(s) for file passthrough")

        return Message(
            role=A2ARole.user,
            parts=parts,
            message_id=str(uuid.uuid4()),
            context_id=context_id,
            task_id=task_id,
            metadata=message_metadata,
        )

    # NOTE: _wrap_message_with_metadata is inherited from BaseA2ARunnable

    async def astream(
        self, input_data: Dict[str, Any], config: Optional[Dict[str, Any]] = None
    ) -> AsyncIterable[StreamEvent]:
        """Stream A2A status updates in real-time.

        Yields status updates as they arrive from the A2A service, enabling
        real-time progress reporting to end users.

        Note: Streaming operations cannot be retried mid-stream. If a connection
        fails, the entire operation must be restarted by the caller.

        Args:
            input_data: Input containing messages and a2a_tracking state
            config: Optional RunnableConfig for LangChain callback/tracing propagation.
                Not used directly by the remote client (trace headers are injected
                via httpx event hooks), but accepted for interface consistency with
                LangChain's Runnable.astream() and LocalA2ARunnable.astream().

        Yields:
            Status update dictionaries with type, state, and data
        """
        logger.debug("========== A2A STREAM START ==========")

        try:
            # Get client and prepare message
            client = await self._get_client()
            input_data_validated = SubAgentInput.model_validate(input_data)
            content, file_blocks = self._extract_content_and_file_blocks(input_data_validated)
            context_id, task_id = self._extract_tracking_ids(input_data_validated)
            a2a_message = self._create_a2a_message(
                content,
                context_id,
                task_id,
                file_blocks=file_blocks,
                scheduled_job_id=input_data_validated.scheduled_job_id,
            )

            logger.info(f"Streaming A2A message: {a2a_message.message_id}")

            # Stream responses from A2A service
            message_count = 0
            task_updates_count = 0
            MAX_MESSAGES = 1000  # Prevent infinite message loops

            logger.info("[STREAMING] A2A client starting to iterate over client.send_message() stream")
            try:
                async for item in client.send_message(a2a_message):
                    message_count += 1
                    item_type = type(item).__name__
                    logger.info(f"[STREAMING] A2A stream item #{message_count}: {item_type}")

                    # Safety check to prevent infinite loops
                    if message_count > MAX_MESSAGES:
                        logger.warning(f"Stream exceeded maximum message limit ({MAX_MESSAGES}), terminating")
                        yield ErrorEvent(
                            error=f"Stream exceeded maximum message limit ({MAX_MESSAGES})",
                            error_type="StreamLimitExceeded",
                        )
                        break

                    # Handle Message responses - yield immediately
                    if isinstance(item, Message):
                        logger.info(f"[STREAMING] A2A client processing Message response #{message_count}")
                        response = await self._handle_message_response(item)
                        yield TaskUpdate(
                            data=response,
                        )
                        logger.info("[STREAMING] A2A client yielded TaskUpdate from message")

                    # Handle Task status updates - yield immediately
                    elif isinstance(item, tuple) and len(item) == 2:
                        task, update_event = item

                        if not isinstance(task, Task):
                            logger.debug(f"Ignoring non-Task tuple item: {type(task)}")
                            continue

                        # Handle artifact streaming events (TaskArtifactUpdateEvent)
                        if isinstance(update_event, TaskArtifactUpdateEvent):
                            text_parts = []
                            for part in update_event.artifact.parts:
                                if part.root.kind == "text":
                                    text_parts.append(part.root.text)
                            text_content = "".join(text_parts)
                            if text_content:
                                logger.info(
                                    f"[STREAMING] A2A client yielding ArtifactUpdate: {len(text_content)} chars, append={update_event.append}"
                                )
                                yield ArtifactUpdate(
                                    content=text_content,
                                    artifact_id=update_event.artifact.artifact_id,
                                    append=update_event.append,
                                    last_chunk=update_event.last_chunk,
                                    metadata=update_event.artifact.metadata,
                                )
                            continue

                        task_updates_count += 1
                        logger.debug(f"Task state: {task.status.state} (update #{task_updates_count})")
                        task_response = await self._handle_task_response(task)

                        # Yield status update with wrapped metadata
                        wrapped_response = self._wrap_message_with_metadata(task_response)

                        # Extract event-level metadata (e.g. todo_snapshot) from
                        # the TaskStatusUpdateEvent — separate from TaskStatus.
                        raw_event_metadata = getattr(update_event, "metadata", None) or {}
                        event_metadata = parse_event_metadata(raw_event_metadata)

                        # Extract raw status text from A2A protocol (before synthetic wrapping).
                        # This is the actual human-readable message the sub-agent intended to
                        # show, without _create_synthetic_message_content() transformations.
                        raw_status_text = ""
                        if task.status.message and task.status.message.parts:
                            raw_status_text = self._extract_text_from_parts(task.status.message.parts)

                        yield TaskUpdate(
                            data=wrapped_response,
                            event_metadata=event_metadata,
                            status_text=raw_status_text,
                        )

                        logger.info(f"Streamed task update #{task_updates_count}: {task.status.state}")

                        # Terminal or intervention states: final yield and stop
                        if task.status.state in TERMINAL_STATES:
                            logger.info(f"Task reached terminal state: {task.status.state}")
                            break
                        elif task.status.state in [TaskState.auth_required, TaskState.input_required]:
                            logger.info(f"Task requires intervention: {task.status.state}")
                            break

                    else:
                        logger.debug(f"Ignoring unknown item type: {type(item)}")

                logger.info(f"[STREAMING] A2A client stream complete - received {message_count} items total")

            except asyncio.TimeoutError:
                logger.error("A2A stream timed out")
                yield ErrorEvent(
                    error="A2A operation timed out",
                    error_type="TimeoutError",
                    requires_retry=True,
                )
            except Exception as e:
                logger.error(f"A2A stream error: {e}")
                import traceback

                logger.debug(f"Traceback: {traceback.format_exc()}")
                yield ErrorEvent(
                    error=str(e),
                    error_type=type(e).__name__,
                )

            logger.debug("========== A2A STREAM END ==========")

        except Exception as e:
            logger.error(f"A2A stream initialization error: {e}")
            yield ErrorEvent(
                error=str(e),
                error_type=type(e).__name__,
            )

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit with cleanup."""
        if self._http_client and self._close_http_client:
            await self._http_client.aclose()
