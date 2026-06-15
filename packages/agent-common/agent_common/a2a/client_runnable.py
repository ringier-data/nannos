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
import logging
import time
import uuid
from collections.abc import AsyncIterable, Sequence
from typing import Any, Dict, List, Optional

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    Message,
    SendMessageRequest,
    Task,
    TaskState,
)
from a2a.types import (
    Part as A2APart,
)
from a2a.types import (
    Role as A2ARole,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from langchain_core.messages import AIMessage, HumanMessage
from langsmith.run_helpers import get_current_run_tree
from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

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
        """Return the agent description with structured skill/example markup."""
        skills_parts: list[str] = []
        for skill in self.agent_card.skills or []:
            examples_txt = ""
            if skill.examples:
                example_lines = "\n".join(f"  - {ex}" for ex in skill.examples)
                examples_txt = f"\n<examples>\n{example_lines}\n</examples>"
            skills_parts.append(f'<skill name="{skill.name}">\n{skill.description}{examples_txt}\n</skill>')
        skills_txt = ""
        if skills_parts:
            skills_txt = "\n<skills>\n" + "\n".join(skills_parts) + "\n</skills>"
        full_description = f"{self.agent_card.description or ''}{skills_txt}"
        return full_description.strip() or "An A2A agent."

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
            )
            factory = ClientFactory(client_config)
            interceptors = [self.config.auth_interceptor] if self.config.auth_interceptor else None
            self._client = factory.create(self.agent_card, interceptors=interceptors)  # type: ignore

        return self._client

    def _extract_text_from_parts(self, parts: Sequence[A2APart]) -> str:
        """Extract text content from A2A parts."""
        return a2a_parts_to_content(parts, text_only=True)

    def _parse_auth_payload(self, task_status) -> Dict[str, Any]:
        """Parse authentication payload from task status following CIBA patterns."""
        message_text = "Authentication required for downstream service"
        service_name = "unknown_service"  # TODO: the application should provide this
        auth_methods = []

        # Extract information from task status message
        if task_status.HasField("message") and task_status.message.parts:
            message_text = self._extract_text_from_parts(task_status.message.parts)

            # Try to parse structured auth info from message metadata
            try:
                for part in task_status.message.parts:
                    if part.WhichOneof("content") == "data":
                        auth_data = MessageToDict(part.data)
                        logger.info(f"Parsing structured auth data: {auth_data}")
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
            correlation_id=task_status.message.message_id if task_status.HasField("message") else None,
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
        """Process a full Task snapshot into a ``TaskResponseData``."""
        return await self._task_response(task.id, task.context_id, task.status, task.artifacts)

    async def _task_response(
        self,
        task_id: str,
        context_id: str,
        status: Any,
        artifacts: Optional[Sequence[Any]] = None,
    ) -> TaskResponseData:
        """
        Build a ``TaskResponseData`` from A2A task lifecycle fields.

        Works from either a full ``Task`` (task snapshot) or a
        ``TaskStatusUpdateEvent`` (carrying only ``TaskStatus`` + ids), since
        A2A v1.0+ delivers these as separate ``StreamResponse`` payloads.
        """
        # Application-specific metadata (separate from A2A protocol)
        app_metadata: dict[str, Any] = {}

        if status.state == TaskState.TASK_STATE_AUTH_REQUIRED:
            app_metadata.update(self._parse_auth_payload(status))

        # Create synthetic message for LangChain compatibility
        content = self._synthetic_content(status, artifacts, app_metadata)
        messages = [AIMessage(content=content)]
        logger.debug(f"Added synthetic message: {content}")

        return TaskResponseData(
            task_id=task_id,
            context_id=context_id,
            state=status.state,
            messages=messages,
            metadata=app_metadata if app_metadata else {},
        )

    @staticmethod
    def _extract_parts(parts: Sequence[A2APart]) -> list[Dict[str, Any]]:
        """Extract A2A parts into plain dicts.

        Works with both ``Message.parts`` and ``Artifact.parts``. In A2A v1.0+
        a ``Part`` is a flat protobuf message; the populated ``content`` oneof
        field (text / raw / url / data) determines the kind.
        """
        parts_data: list[Dict[str, Any]] = []
        for part in parts:
            kind = part.WhichOneof("content")
            metadata = MessageToDict(part.metadata) if part.HasField("metadata") else {}
            if kind == "text":
                parts_data.append({"type": "text", "content": part.text, "metadata": metadata})
            elif kind in ("url", "raw"):
                parts_data.append(
                    {
                        "type": "file",
                        "file": part.url if kind == "url" else part.raw,
                        "media_type": part.media_type,
                        "metadata": metadata,
                    }
                )
            elif kind == "data":
                parts_data.append({"type": "data", "content": MessageToDict(part.data), "metadata": metadata})
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

    def _synthetic_content(self, status: Any, artifacts: Optional[Sequence[Any]], app_metadata: Dict[str, Any]) -> str:
        """Create synthetic message content following A2A protocol.

        For failed/incomplete tasks, explicitly indicates the state to ensure
        the LLM recognizes when a task did not complete successfully.

        DEFENSIVE DESIGN: Treats non-terminal states (working, input_required, etc.)
        as incomplete to prevent the orchestrator from incorrectly assuming success.

        Operates on a ``TaskStatus`` (+ optional artifacts) so it works for both
        full-Task and status-update stream payloads.
        """
        content = "Task processed"

        # A2A protocol: status.message contains human-readable details
        if status.HasField("message") and status.message.parts:
            content = self._extract_text_from_parts(status.message.parts)
        elif status.state == TaskState.TASK_STATE_AUTH_REQUIRED:
            content = app_metadata.get("instructions", "Authentication required")
        elif artifacts:
            # Use first artifact content if available (A2A protocol)
            first_artifact = artifacts[0]
            if first_artifact.parts:
                first_part = first_artifact.parts[0]
                if first_part.WhichOneof("content") == "text":
                    content = first_part.text or "Task completed"

        # CRITICAL: For non-successful states, explicitly indicate the issue
        # This ensures the LLM understands the task did not complete successfully
        if status.state == TaskState.TASK_STATE_FAILED:
            # If content doesn't already indicate failure, prepend error marker
            lower_content = content.lower()
            if "failed" not in lower_content and "error" not in lower_content:
                content = f"ERROR: Task failed - {content}"
            else:
                # Content already mentions failure, but ensure it's prominent
                content = f"Task execution failed: {content}"
        elif status.state == TaskState.TASK_STATE_WORKING:
            # Agent is still processing - this should NOT be treated as completion
            content = f"INCOMPLETE: Agent is still working - {content}"
        elif status.state not in TERMINAL_STATES:
            # Any other non-terminal state (input_required, auth_required, etc.)
            # Prepend state information for clarity
            state_name = TaskState.Name(status.state)
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
                "role": A2ARole.Name(message.role),
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

                if state not in TERMINAL_STATES and state not in (TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED):
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

    def _from_human_messages_to_a2a(
        self,
        human_messages: List[HumanMessage],
        context_id: Optional[str],
        task_id: Optional[str],
        scheduled_job_id: Optional[int] = None,
    ) -> Message:
        """Transform a list of LangChain HumanMessages to a single A2A Message.

        Processes all messages in order, aggregating their parts into a single
        A2A message while preserving content block order. This enables multi-turn
        conversations to be sent as a complete message thread.

        Args:
            human_messages: List of LangChain HumanMessage objects
            context_id: Optional context ID for multi-turn conversations
            task_id: Optional task ID for continuing existing tasks
            scheduled_job_id: Optional scheduled job ID for cost attribution

        Returns:
            Constructed A2A Message aggregating parts from all HumanMessages
        """
        message_metadata: Dict[str, Any] = {
            "source": "Orchestrator",
            "timestamp": time.time(),
        }
        if scheduled_job_id is not None:
            message_metadata["scheduled_job_id"] = scheduled_job_id

        all_parts: list[A2APart] = []

        # Process each message in order, aggregating parts while preserving order
        for human_message in human_messages:
            # Handle content_blocks if present (structured input)
            if hasattr(human_message, "content") and isinstance(human_message.content, list):
                for block in human_message.content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "")

                        if block_type == "text":
                            # Text block -> text Part
                            text = block.get("text", "")
                            if text:
                                all_parts.append(A2APart(text=text, metadata=message_metadata))

                        elif block_type == "non_standard":
                            # JSON block (non_standard) -> data Part
                            value = block.get("value", {})
                            if isinstance(value, dict) and value.get("media_type") == "application/json":
                                json_data = value.get("data", {})
                                all_parts.append(
                                    A2APart(
                                        data=ParseDict(json_data, Value()),
                                        metadata={"media_type": "application/json", **message_metadata},
                                    )
                                )

                        elif block_type in ("image", "audio", "video", "file"):
                            # File block -> file Part
                            file_part = self._content_block_to_file_part(block)
                            if file_part:
                                all_parts.append(file_part)
                        else:
                            logger.warning(f"Unsupported content block type: {block_type}. Skipping block.")
            else:
                # Plain text content
                content = (
                    human_message.content if isinstance(human_message.content, str) else str(human_message.content)
                )
                if content:
                    all_parts.append(A2APart(text=content, metadata=message_metadata))

        return Message(
            role=A2ARole.ROLE_USER,
            parts=all_parts,
            message_id=str(uuid.uuid4()),
            context_id=context_id or "",
            task_id=task_id or "",
            metadata=message_metadata,
        )

    @staticmethod
    def _content_block_to_file_part(block: dict) -> Optional[A2APart]:
        """Convert a LangChain content block dict to an A2A file Part.

        Args:
            block: Dict with keys like 'type', 'url', 'mime_type'

        Returns:
            A2A file Part (``url`` + ``media_type``), or None if URL is missing
        """
        url = block.get("url", "")
        if not url:
            return None
        mime_type = block.get("mime_type")
        if mime_type:
            return A2APart(url=url, media_type=mime_type)
        return A2APart(url=url)

    # NOTE: _wrap_message_with_metadata is inherited from BaseA2ARunnable

    async def send_steering_message(self, message: Message) -> None:
        """Send a steering message to the sub-agent and consume the ack.

        When the sub-agent has an active stream for the same context_id, its
        executor queues the message and returns an immediate acknowledgment.
        This method sends the message and drains the ack response.

        Args:
            message: A2A Message with context_id/task_id set to the active task.
        """
        client = await self._get_client()
        logger.info(
            f"[STEERING] Forwarding steering message to {self.name} "
            f"(context_id={message.context_id}, task_id={message.task_id})"
        )
        try:
            async for _ in client.send_message(SendMessageRequest(message=message)):
                pass  # drain ack events
        except Exception:
            logger.warning(
                f"[STEERING] Failed to forward steering message to {self.name}",
                exc_info=True,
            )

    async def cancel_task(self, task_id: str) -> None:
        """Send an A2A tasks/cancel request to the remote agent.

        Best-effort: logs warnings on failure but never raises.

        Args:
            task_id: The A2A task ID to cancel.
        """
        try:
            client = await self._get_client()
            logger.info(f"Sending cancel_task to {self.name} (task_id={task_id})")
            await client.cancel_task(CancelTaskRequest(id=task_id))
            logger.info(f"cancel_task acknowledged by {self.name} (task_id={task_id})")
        except Exception:
            logger.warning(
                f"Failed to cancel task on {self.name} (task_id={task_id})",
                exc_info=True,
            )

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

            # Transform all HumanMessages to a single A2A Message
            context_id, task_id = self._extract_tracking_ids(input_data_validated)

            if not input_data_validated.messages:
                raise ValueError("No messages in input")

            a2a_message = self._from_human_messages_to_a2a(
                input_data_validated.messages,
                context_id,
                task_id,
                scheduled_job_id=input_data_validated.scheduled_job_id,
            )

            logger.info(f"Streaming A2A message: {a2a_message.message_id}")

            # Stream responses from A2A service
            message_count = 0
            task_updates_count = 0
            MAX_MESSAGES = 1000  # Prevent infinite message loops

            logger.info("[STREAMING] A2A client starting to iterate over client.send_message() stream")
            request = SendMessageRequest(message=a2a_message)
            # In A2A v1.0+ the client yields StreamResponse objects whose `payload`
            # oneof is one of {task, message, status_update, artifact_update}.
            # status_update events carry only TaskStatus + ids, so we track the
            # most recent full Task to supply artifacts to status-derived responses.
            latest_task: Optional[Task] = None
            try:
                async for chunk in client.send_message(request):
                    message_count += 1
                    payload = chunk.WhichOneof("payload")
                    logger.info(f"[STREAMING] A2A stream item #{message_count}: {payload}")

                    # Safety check to prevent infinite loops
                    if message_count > MAX_MESSAGES:
                        logger.warning(f"Stream exceeded maximum message limit ({MAX_MESSAGES}), terminating")
                        yield ErrorEvent(
                            error=f"Stream exceeded maximum message limit ({MAX_MESSAGES})",
                            error_type="StreamLimitExceeded",
                        )
                        break

                    # Handle Message responses - yield immediately
                    if payload == "message":
                        logger.info(f"[STREAMING] A2A client processing Message response #{message_count}")
                        response = await self._handle_message_response(chunk.message)
                        yield TaskUpdate(data=response)
                        logger.info("[STREAMING] A2A client yielded TaskUpdate from message")
                        continue

                    # Handle artifact streaming events
                    if payload == "artifact_update":
                        ev = chunk.artifact_update
                        text_parts = [p.text for p in ev.artifact.parts if p.WhichOneof("content") == "text"]
                        text_content = "".join(text_parts)
                        if text_content:
                            logger.info(
                                f"[STREAMING] A2A client yielding ArtifactUpdate: {len(text_content)} chars, append={ev.append}"
                            )
                            yield ArtifactUpdate(
                                content=text_content,
                                artifact_id=ev.artifact.artifact_id,
                                append=ev.append,
                                last_chunk=ev.last_chunk,
                                metadata=MessageToDict(ev.artifact.metadata) if ev.artifact.HasField("metadata") else None,
                            )
                        continue

                    # Handle full-task snapshots and status updates
                    if payload in ("task", "status_update"):
                        if payload == "task":
                            latest_task = chunk.task
                            task_id, context_id, status = latest_task.id, latest_task.context_id, latest_task.status
                            artifacts = latest_task.artifacts
                            raw_event_metadata: dict[str, Any] = {}
                        else:
                            ev = chunk.status_update
                            task_id, context_id, status = ev.task_id, ev.context_id, ev.status
                            artifacts = latest_task.artifacts if latest_task else None
                            # Event-level metadata (e.g. todo_snapshot), separate from TaskStatus.
                            raw_event_metadata = MessageToDict(ev.metadata) if ev.HasField("metadata") else {}

                        task_updates_count += 1
                        logger.debug(f"Task state: {status.state} (update #{task_updates_count})")
                        task_response = await self._task_response(task_id, context_id, status, artifacts)

                        # Yield status update with wrapped metadata
                        wrapped_response = self._wrap_message_with_metadata(task_response)
                        event_metadata = parse_event_metadata(raw_event_metadata)

                        # Extract raw status text from A2A protocol (before synthetic wrapping).
                        raw_status_text = ""
                        if status.HasField("message") and status.message.parts:
                            raw_status_text = self._extract_text_from_parts(status.message.parts)

                        yield TaskUpdate(
                            data=wrapped_response,
                            event_metadata=event_metadata,
                            status_text=raw_status_text,
                        )

                        logger.info(f"Streamed task update #{task_updates_count}: {status.state}")

                        # Terminal or intervention states: final yield and stop
                        if status.state in TERMINAL_STATES:
                            logger.info(f"Task reached terminal state: {status.state}")
                            break
                        elif status.state in (TaskState.TASK_STATE_AUTH_REQUIRED, TaskState.TASK_STATE_INPUT_REQUIRED):
                            logger.info(f"Task requires intervention: {status.state}")
                            break
                        continue

                    logger.debug(f"Ignoring unknown stream payload: {payload}")

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
