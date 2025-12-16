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
import uuid
from collections.abc import AsyncIterable, Sequence
from typing import Any, Dict, Optional

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    Task,
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
from langchain_core.messages import AIMessage
from langsmith.run_helpers import get_current_run_tree

from ..authentication import (
    AuthenticationMethod,
    AuthPayload,
    ServiceAuthRequirement,
)
from .base import BaseA2ARunnable, SubAgentInput
from .config import A2AClientConfig

logger = logging.getLogger(__name__)

# Terminal task states that indicate task completion
TERMINAL_TASK_STATES = [
    TaskState.completed,
    TaskState.failed,
    TaskState.canceled,
    TaskState.rejected,
]


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

    async def _handle_task_response(self, task: Task) -> Dict[str, Any]:
        """
        Process task response using clean A2A protocol compliance.

        Uses A2A SDK types directly and follows protocol specification.
        Returns dict for LangChain/DeepAgents compatibility.
        """
        from .models import A2ATaskResponse

        # Application-specific metadata (separate from A2A protocol)
        app_metadata = {}

        # Handle different task states following A2A protocol
        if task.status.state == TaskState.auth_required:
            auth_payload = self._parse_auth_payload(task.status)
            app_metadata.update(auth_payload)

        # Create synthetic message for LangChain compatibility
        content = self._create_synthetic_message_content(task, app_metadata)
        messages = [AIMessage(content=content)]
        logger.debug(f"Added synthetic message: {content}")

        # Create A2A response model
        response = A2ATaskResponse(task=task, app_metadata=app_metadata, messages=messages)

        # Return dict for LangChain/DeepAgents compatibility
        return {
            "task_id": task.id,
            "context_id": task.context_id,
            "state": task.status.state,
            "artifacts": self._extract_artifacts_data(task),
            "is_complete": response.is_complete,
            "requires_auth": response.requires_auth,
            "requires_input": response.requires_input,
            "messages": messages,
            **app_metadata,
        }

    def _extract_artifacts_data(self, task: Task) -> list[Dict[str, Any]]:
        """Extract artifacts data following A2A protocol structure."""
        artifacts_data = []

        if task.artifacts:
            for artifact in task.artifacts:
                artifact_data = {
                    "id": artifact.artifact_id,
                    "name": artifact.name,
                    "description": artifact.description,
                    "parts": [],
                }
                for part in artifact.parts:
                    inner_part = part.root
                    if inner_part.kind == "text":
                        artifact_data["parts"].append(
                            {
                                "type": "text",
                                "content": inner_part.text,
                                "metadata": inner_part.metadata or {},
                            }
                        )
                    elif inner_part.kind == "file":
                        artifact_data["parts"].append(
                            {
                                "type": "file",
                                "file": inner_part.file,
                                "metadata": inner_part.metadata or {},
                            }
                        )
                    elif inner_part.kind == "data":
                        artifact_data["parts"].append(
                            {
                                "type": "data",
                                "content": inner_part.data,
                                "metadata": inner_part.metadata or {},
                            }
                        )
                artifacts_data.append(artifact_data)

        return artifacts_data

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
        elif task.status.state not in TERMINAL_TASK_STATES:
            # Any other non-terminal state (input_required, auth_required, etc.)
            # Prepend state information for clarity
            state_name = task.status.state.value if hasattr(task.status.state, "value") else str(task.status.state)
            content = f"Agent status: {state_name} - {content}"

        return content

    async def _handle_message_response(self, message: Message) -> Dict[str, Any]:
        """
        Process message response using clean A2A protocol compliance.
        """
        # Return dict for compatibility
        return {
            "message_id": message.message_id,
            "role": message.role,
            "context_id": message.context_id,
            "task_id": message.task_id,
            "parts": self._extract_message_parts(message),
        }

    def _extract_message_parts(self, message: Message) -> list[Dict[str, Any]]:
        """Extract message parts following A2A protocol structure."""
        parts_data = []

        for part in message.parts:
            inner_part = part.root
            if inner_part.kind == "text":
                parts_data.append(
                    {
                        "type": "text",
                        "content": inner_part.text,
                        "metadata": inner_part.metadata or {},
                    }
                )
            elif inner_part.kind == "file":
                parts_data.append(
                    {
                        "type": "file",
                        "file": inner_part.file,
                        "metadata": inner_part.metadata or {},
                    }
                )
            elif inner_part.kind == "data":
                parts_data.append(
                    {
                        "type": "data",
                        "content": inner_part.data,
                        "metadata": inner_part.metadata or {},
                    }
                )

        return parts_data

    async def ainvoke(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Non-streaming invoke by collecting all stream updates.

        Backwards compatible with existing code that expects a single result.
        Internally uses astream() and collects the final result.

        IMPORTANT: If the stream ends without reaching a terminal state (completed, failed,
        canceled, rejected), we treat it as a failure. This handles cases where the sub-agent
        crashes or disconnects before emitting a final status.

        Note: May log SSE cleanup warnings ("generator didn't stop after athrow()") which are
        cosmetic and don't affect functionality. These occur when asyncio.run() tears down the
        event loop before the A2A library's SSE connection fully cleans up.
        """
        final_result = {}
        last_state = None

        try:
            async for item in self.astream(input_data):
                # Keep updating with latest data
                if item.get("type") == "task_update" or item.get("type") == "message":
                    final_result = item.get("data", {})
                    last_state = item.get("state")
                # For errors, return error response immediately
                elif item.get("type") == "error":
                    return {
                        "error": item.get("error"),
                        "error_type": item.get("error_type"),
                        "is_complete": True,  # Mark as complete since it's a terminal error
                        "state": str(TaskState.failed),
                        "requires_retry": item.get("requires_retry", True),
                        "messages": [AIMessage(content=f"Error: {item.get('error')}")],
                    }

            # CRITICAL: Check if stream ended without reaching a terminal state
            # This handles cases where sub-agent crashes or disconnects unexpectedly
            if final_result:
                result_state = final_result.get("state")
                # Convert state string back to TaskState for comparison
                state_str = str(result_state) if result_state else last_state

                # Check if we ended in a non-terminal state
                is_terminal = any(
                    terminal.value in str(state_str).lower() or str(terminal) in str(state_str)
                    for terminal in TERMINAL_TASK_STATES
                )
                is_intervention = "input_required" in str(state_str) or "auth_required" in str(state_str)

                if not is_terminal and not is_intervention:
                    # Stream ended without terminal state - treat as unexpected failure
                    logger.warning(
                        f"A2A stream ended with non-terminal state: {state_str}. "
                        "Sub-agent may have crashed or disconnected. Treating as failure."
                    )

                    # Update the result to indicate failure
                    original_content = ""
                    if final_result.get("messages"):
                        last_msg = final_result["messages"][-1]
                        if hasattr(last_msg, "content"):
                            original_content = last_msg.content

                    error_content = (
                        f"The agent stopped responding unexpectedly. Last status: {state_str}. {original_content}"
                    )

                    final_result["state"] = str(TaskState.failed)
                    final_result["is_complete"] = True
                    final_result["messages"] = [AIMessage(content=error_content)]

            return final_result or {
                "is_complete": True,
                "state": str(TaskState.failed),
                "messages": [AIMessage(content="No response received from agent")],
            }
        except httpx.ConnectError as e:
            logger.error(f"A2A connection failed: {e}")

            return {
                "error": "Unable to connect to A2A service. The service may be offline.",
                "error_type": "ConnectionError",
                "is_complete": False,
                "requires_retry": False,
                "timestamp": asyncio.get_event_loop().time(),
                "messages": [
                    AIMessage(content="The requested service is currently unavailable. Please try again later.")
                ],
            }
        except httpx.TimeoutException as e:
            logger.error(f"A2A request timed out: {e}")

            return {
                "error": "A2A request timed out. The service may be slow or unavailable.",
                "error_type": "TimeoutError",
                "is_complete": False,
                "requires_retry": True,
                "timestamp": asyncio.get_event_loop().time(),
                "messages": [AIMessage(content="The request timed out. Please try again.")],
            }
        except Exception as e:
            logger.error(f"A2A invocation failed: {e}")

            # Provide user-friendly error message
            error_msg = str(e)
            user_friendly_msg = "An error occurred while processing your request."

            if "HTTP" in error_msg and ("500" in error_msg or "502" in error_msg or "503" in error_msg):
                user_friendly_msg = "The service is temporarily unavailable. Please try again later."

            return {
                "error": error_msg,
                "error_type": type(e).__name__,
                "is_complete": False,
                "requires_retry": True,
                "timestamp": asyncio.get_event_loop().time(),
                "messages": [AIMessage(content=user_friendly_msg)],
            }

    def stream(self, input_data: Dict[str, Any]):
        """Synchronous streaming is not supported - use astream instead.

        Raises:
            NotImplementedError: Always, as sync streaming is not supported
        """
        raise NotImplementedError("Synchronous streaming not supported for A2A. Use astream() instead.")

    def _create_a2a_message(self, content: str, context_id: Optional[str], task_id: Optional[str]) -> Message:
        """Create an A2A message with proper metadata.

        Args:
            content: Message content
            context_id: Optional context ID for multi-turn conversations
            task_id: Optional task ID for continuing existing tasks

        Returns:
            Constructed A2A Message
        """
        message_metadata = {
            "source": "Orchestrator",
            "timestamp": asyncio.get_event_loop().time(),
        }

        parts = [A2APart(root=TextPart(text=content, metadata=message_metadata))]

        return Message(
            role=A2ARole.user,
            parts=parts,
            message_id=str(uuid.uuid4()),
            context_id=context_id,
            task_id=task_id,
            metadata=message_metadata,
        )

    # NOTE: _wrap_message_with_metadata is inherited from BaseA2ARunnable

    async def astream(self, input_data: Dict[str, Any]) -> AsyncIterable[Dict[str, Any]]:
        """Stream A2A status updates in real-time.

        Yields status updates as they arrive from the A2A service, enabling
        real-time progress reporting to end users.

        Note: Streaming operations cannot be retried mid-stream. If a connection
        fails, the entire operation must be restarted by the caller.

        Args:
            input_data: Input containing messages and a2a_tracking state

        Yields:
            Status update dictionaries with type, state, and data
        """
        logger.debug("========== A2A STREAM START ==========")

        try:
            # Get client and prepare message
            client = await self._get_client()
            input_data_validated = SubAgentInput.model_validate(input_data)
            content = self._extract_message_content(input_data_validated)
            context_id, task_id = self._extract_tracking_ids(input_data_validated)
            a2a_message = self._create_a2a_message(content, context_id, task_id)

            logger.info(f"Streaming A2A message: {a2a_message.message_id}")

            # Stream responses from A2A service
            message_count = 0
            task_updates_count = 0
            MAX_MESSAGES = 100  # Prevent infinite message loops

            try:
                async for item in client.send_message(a2a_message):
                    message_count += 1
                    logger.debug(f"Stream item #{message_count}: {type(item).__name__}")

                    # Safety check to prevent infinite loops
                    if message_count > MAX_MESSAGES:
                        logger.warning(f"Stream exceeded maximum message limit ({MAX_MESSAGES}), terminating")
                        yield {
                            "type": "error",
                            "error": f"Stream exceeded maximum message limit ({MAX_MESSAGES})",
                            "error_type": "StreamLimitExceeded",
                            "is_complete": True,
                            "message_count": message_count,
                        }
                        break

                    # Handle Message responses - yield immediately
                    if isinstance(item, Message):
                        response = await self._handle_message_response(item)
                        yield {
                            "type": "message",
                            "data": response,
                            "is_complete": False,
                            "message_count": message_count,
                        }
                        logger.info(f"Streamed message response #{message_count}")

                    # Handle Task status updates - yield immediately
                    elif isinstance(item, tuple) and len(item) == 2:
                        task, update_event = item

                        if not isinstance(task, Task):
                            logger.debug(f"Ignoring non-Task tuple item: {type(task)}")
                            continue

                        task_updates_count += 1
                        logger.debug(f"Task state: {task.status.state} (update #{task_updates_count})")
                        task_response = await self._handle_task_response(task)

                        # Yield status update with wrapped metadata
                        wrapped_response = self._wrap_message_with_metadata(task_response)

                        yield {
                            "type": "task_update",
                            "state": str(task.status.state),
                            "data": wrapped_response,
                            "is_complete": task.status.state in TERMINAL_TASK_STATES,
                            "requires_input": task.status.state in [TaskState.auth_required, TaskState.input_required],
                            "message_count": message_count,
                            "task_updates_count": task_updates_count,
                        }

                        logger.info(f"Streamed task update #{task_updates_count}: {task.status.state}")

                        # Terminal or intervention states: final yield and stop
                        if task.status.state in TERMINAL_TASK_STATES:
                            logger.info(f"Task reached terminal state: {task.status.state}")
                            break
                        elif task.status.state in [TaskState.auth_required, TaskState.input_required]:
                            logger.info(f"Task requires intervention: {task.status.state}")
                            break

                    else:
                        logger.debug(f"Ignoring unknown item type: {type(item)}")

            except asyncio.TimeoutError:
                logger.error("A2A stream timed out")
                yield {
                    "type": "error",
                    "error": "A2A operation timed out",
                    "error_type": "TimeoutError",
                    "is_complete": False,
                    "requires_retry": True,
                    "message_count": message_count,
                }
            except Exception as e:
                logger.error(f"A2A stream error: {e}")
                import traceback

                logger.debug(f"Traceback: {traceback.format_exc()}")
                yield {
                    "type": "error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "is_complete": False,
                    "message_count": message_count,
                }

            logger.debug("========== A2A STREAM END ==========")

        except Exception as e:
            logger.error(f"A2A stream initialization error: {e}")
            yield {
                "type": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "is_complete": False,
            }

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit with cleanup."""
        if self._http_client and self._close_http_client:
            await self._http_client.aclose()
