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
from langchain_core.messages import HumanMessage
from langsmith.run_helpers import get_current_run_tree

from agent_common.a2a.base import BaseA2ARunnable, SubAgentInput
from agent_common.a2a.config import A2AClientConfig
from agent_common.a2a.event_translation import (
    A2AStreamTranslator,
    extract_parts,
    extract_text_from_parts,
    message_response,
    parse_auth_payload,
    synthetic_content,
    task_response,
)
from agent_common.a2a.message_conversion import content_block_to_file_part, human_messages_to_a2a_message
from agent_common.a2a.stream_events import (
    TERMINAL_STATES,
    ErrorEvent,
    StreamEvent,
    TaskResponseData,
    TaskUpdate,
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

    # The event/status readers below are the shared ones in
    # ``agent_common.a2a.event_translation`` — the in-process server's client side
    # reads the same events through the same functions. Kept as methods so the
    # remote client's surface (and its tests) stays where it was.

    def _extract_text_from_parts(self, parts: Sequence[A2APart]) -> str:
        """Extract text content from A2A parts."""
        return extract_text_from_parts(parts)

    def _parse_auth_payload(self, task_status) -> Dict[str, Any]:
        """Parse authentication payload from task status following CIBA patterns."""
        return parse_auth_payload(task_status)

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
        """Build a ``TaskResponseData`` from A2A task lifecycle fields."""
        return task_response(task_id, context_id, status, artifacts)

    @staticmethod
    def _extract_parts(parts: Sequence[A2APart]) -> list[Dict[str, Any]]:
        """Extract A2A parts into plain dicts."""
        return extract_parts(parts)

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
        """Create synthetic message content following A2A protocol (state made explicit)."""
        return synthetic_content(status, artifacts, app_metadata)

    async def _handle_message_response(self, message: Message) -> TaskResponseData:
        """Convert an A2A Message response into a TaskResponseData."""
        return message_response(message)

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
        message_formatting: Optional[str] = None,
    ) -> Message:
        """Transform a list of LangChain HumanMessages to a single A2A Message.

        The conversion itself is ``agent_common.a2a.message_conversion
        .human_messages_to_a2a_message`` — shared with the in-process server path
        so both sides of every A2A boundary agree on it.
        """
        return human_messages_to_a2a_message(
            human_messages,
            context_id,
            task_id,
            scheduled_job_id=scheduled_job_id,
            message_formatting=message_formatting,
        )

    @staticmethod
    def _content_block_to_file_part(block: dict) -> Optional[A2APart]:
        """Convert a LangChain content block dict to an A2A file Part."""
        return content_block_to_file_part(block)

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
                message_formatting=input_data_validated.message_formatting,
            )

            logger.info(f"Streaming A2A message: {a2a_message.message_id}")

            # Stream responses from A2A service
            message_count = 0
            MAX_MESSAGES = 1000  # Prevent infinite message loops

            logger.info("[STREAMING] A2A client starting to iterate over client.send_message() stream")
            request = SendMessageRequest(message=a2a_message)
            # In A2A v1.0+ the client yields StreamResponse objects whose `payload`
            # oneof is one of {task, message, status_update, artifact_update}. The
            # translation into StreamEvents is shared with the in-process path.
            translator = A2AStreamTranslator()
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

                    if not payload:
                        logger.debug("Ignoring empty stream payload")
                        continue
                    for event in translator.translate(getattr(chunk, payload)):
                        yield event
                    if translator.finished:
                        logger.info(f"Task reached a terminal or intervention state after {message_count} items")
                        break

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
