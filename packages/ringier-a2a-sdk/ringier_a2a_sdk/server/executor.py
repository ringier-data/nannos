"""Base agent executor for A2A protocol.

Supports A2A Continuous Interaction Turns: when a second request arrives for
a context_id that already has an active stream, the new message is queued for
the running agent instead of starting a second execution.  The caller receives
an immediate acknowledgment (current task status).
"""

import asyncio
import logging
import time
import uuid
from abc import ABC
from dataclasses import dataclass, field
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    InvalidParamsError,
    Message,
    Part,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from a2a.utils import (
    new_agent_text_message,
    new_task,
)
from a2a.utils.errors import ServerError

from ..models import UserConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Maximum number of pending steering messages per active stream
MAX_STEERING_QUEUE_DEPTH = 5

# Maximum number of times the executor will re-invoke the agent to process
# steering messages that arrived after the last abefore_model call.
MAX_STEERING_REINVOCATIONS = 1


@dataclass
class ActiveStreamInfo:
    """Tracks an active agent execution stream for a given context_id.

    Used by the executor to detect concurrent requests and route
    additional messages to the running agent's steering queue.
    """

    context_id: str
    task_id: str
    owner_sub: str | None = None
    assistant_id: str | None = None
    scope: str | None = None  # "personal" or "channel"
    message_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    started_at: float = field(default_factory=time.time)


# Module-level registry of active streams, keyed by context_id.
# Access is safe from a single asyncio event loop (no threading lock needed).
_active_streams: dict[str, ActiveStreamInfo] = {}
_active_streams_lock = asyncio.Lock()


class BaseAgentExecutor(AgentExecutor, ABC):
    """Base executor for A2A agents.

    Handles the execution flow for agent tasks including:
    - User authentication validation
    - Task creation and updates
    - Stream handling from agent
    - State management following A2A protocol
    """

    def __init__(self, agent: Any) -> None:
        """Initialize executor with an agent instance.

        Args:
            agent: An instance implementing BaseAgent interface
        """
        self.agent = agent

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Execute the agent task.

        Supports A2A Continuous Interaction Turns:
        - If this context_id already has an active stream, the new message is
          queued for the running agent and the current task status is returned
          immediately (acknowledge-only, no new SSE stream).
        - If no active stream exists, execution proceeds normally and registers
          itself in the active stream registry.

        Authentication:
        - User identity is validated by JWTValidatorMiddleware before this method is called
        - Only authenticated requests with valid JWTs can reach this point
        - User info is available in request.state.user (set by middleware) but not directly
          accessible here since A2A SDK abstracts the request layer
        - User context is extracted from request.state.user by UserContextFromRequestStateMiddleware

        Args:
            context: Request context with user information
            event_queue: Event queue for task updates

        Raises:
            ServerError: If validation fails or execution errors occur
        """
        # Note: Authentication is enforced at the middleware layer
        # All requests reaching this method have already been authenticated
        logger.debug("Executing request from authenticated orchestrator")

        error = self._validate_request(context)
        if error:
            raise ServerError(error=InvalidParamsError())

        message = context.message
        if not message:
            logger.error("No message found in request context")
            raise ServerError(error=InvalidParamsError())
        task = context.current_task
        logger.debug(f"Starting execution for query: {context.get_user_input()}")
        logger.debug(f"Current task: {task}")
        if not task:
            task = new_task(context.message)  # type: ignore
            await event_queue.enqueue_event(task)

        context_id = task.context_id

        # ZERO-TRUST: Extract verified user identity from call_context (set by AuthRequestContextBuilder).
        # This runs BEFORE steering so that (a) unauthenticated requests fail hard even on
        # the steering path, and (b) we can use the verified user_sub for owner checks.
        if context.call_context and hasattr(context.call_context, "state"):
            try:
                user_sub = context.call_context.state["user_sub"]
                user_name = context.call_context.state["user_name"]
                user_email = context.call_context.state["user_email"]
                # user_token is optional - not available in orchestrator JWT auth flow
                user_token = context.call_context.state.get("user_token")
                # sub_agent_id is optional - used for cost tracking attribution
                sub_agent_id = context.call_context.state.get("sub_agent_id")
                # phone_number is optional - may be used by voice agents, but not all agents or flows
                phone_number = context.call_context.state.get("phone_number")
            except KeyError as e:
                logger.error(f"[ZERO-TRUST] Missing expected user context key: {e}")
                raise ServerError(error=InvalidParamsError()) from e
        else:
            logger.error("[ZERO-TRUST] No user context found in call_context - authentication may have failed")
            raise ServerError(error=InvalidParamsError())

        logger.debug(f"[ZERO-TRUST] Executing with verified user_sub: {user_sub}, sub_agent_id: {sub_agent_id}")

        # --- Continuous Interaction Turn: route to active stream if one exists ---
        async with _active_streams_lock:
            active = _active_streams.get(context_id)
            if active is not None:
                # Verify the caller is the same user who started the stream
                if active.owner_sub and user_sub != active.owner_sub:
                    logger.warning(
                        f"[STEERING] Rejected steering for context_id={context_id}: "
                        f"caller_sub={user_sub} does not match stream owner"
                    )
                    raise ServerError(error=InvalidParamsError())
                if active.message_queue.qsize() >= MAX_STEERING_QUEUE_DEPTH:
                    logger.warning(
                        f"[STEERING] Queue full for context_id={context_id} "
                        f"(depth={active.message_queue.qsize()}), rejecting"
                    )
                    raise ServerError(error=InvalidParamsError())
                logger.info(
                    f"[STEERING] Active stream found for context_id={context_id}, "
                    f"queuing message for running agent (queue depth: {active.message_queue.qsize() + 1})"
                )
                active.message_queue.put_nowait(context.message)
                # Acknowledge-only: emit a status-update (NOT a raw Task object)
                # so the SSE response has at least one event, then return.
                # Using TaskStatusUpdateEvent avoids the "Task is already set"
                # error on the client's ClientTaskManager when the event queue
                # is a tapped child that also receives parent events.
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id=task.id,
                        context_id=task.context_id,
                        status=TaskStatus(
                            state=task.status.state,
                            message=task.status.message,
                        ),
                        final=False,
                    )
                )
                return

        # No active stream — register ourselves and proceed with execution
        stream_info = ActiveStreamInfo(context_id=context_id, task_id=task.id, owner_sub=user_sub)
        async with _active_streams_lock:
            _active_streams[context_id] = stream_info

        # Share the message queue reference with the agent so SteeringMiddleware
        # can consume pending messages during graph execution.
        self.agent.set_message_queue(context_id, stream_info.message_queue)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        # Extract optional scheduler overrides from message metadata (set by agent-runner)
        scheduled_job_id_from_meta: int | None = None
        try:
            if context.message and isinstance(context.message.metadata, dict) and context.message.metadata:
                raw_job_id = context.message.metadata.get("scheduled_job_id")
                scheduled_job_id_from_meta = int(raw_job_id) if raw_job_id is not None else None
                if scheduled_job_id_from_meta:
                    logger.info(f"[SCHEDULER] scheduled_job_id={scheduled_job_id_from_meta}")
        except Exception as meta_err:
            logger.warning(f"Failed to read message metadata: {meta_err}")

        try:
            # Create config for agent execution
            # Note: access_token is None for orchestrator JWT auth (agent uses orchestrator's JWT)
            user_config = UserConfig(
                user_sub=user_sub,
                access_token=user_token,  # May be None in JWT auth flow
                name=user_name,
                email=user_email,
                sub_agent_id=sub_agent_id,  # For cost tracking attribution
                scheduled_job_id=scheduled_job_id_from_meta,  # For scheduled-job cost attribution
                phone_number=phone_number,
            )
            steering_reinvocations = 0
            messages: list[Message] = [message]

            while True:
                streaming_artifact_id = str(uuid.uuid4())
                first_chunk_sent = False  # Track if we've sent the initial main artifact
                first_intermediate_chunk_sent = False  # Track if we've sent the initial intermediate artifact
                deferred_terminal_item = None

                async for item in self.agent.stream(messages, user_config, task):
                    # Buffer the terminal completed item so we can check for unconsumed
                    # steering messages before emitting it.  Other terminal states
                    # (failed, input_required, auth_required) are emitted immediately
                    # since re-invocation only makes sense after successful completion.
                    if item.state == TaskState.completed:
                        deferred_terminal_item = item
                        continue
                    first_chunk_sent, first_intermediate_chunk_sent = await self._handle_stream_item(
                        item, updater, task, streaming_artifact_id, first_chunk_sent, first_intermediate_chunk_sent
                    )

                # Check for steering messages that arrived after the last abefore_model
                if (
                    deferred_terminal_item is not None
                    and deferred_terminal_item.state == TaskState.completed
                    and steering_reinvocations < MAX_STEERING_REINVOCATIONS
                ):
                    try:
                        unconsumed = self.agent.get_pending_messages(context_id)
                        if unconsumed and isinstance(unconsumed, list) and len(unconsumed) > 0:
                            steering_reinvocations += 1
                            messages = unconsumed  # will be processed in the next stream round
                            logger.info(
                                f"[STEERING] Re-invoking agent with {len(unconsumed)} late steering "
                                f"message(s) (reinvocation {steering_reinvocations}/{MAX_STEERING_REINVOCATIONS})"
                            )
                            continue  # Loop back for another stream round
                    except Exception:
                        pass  # Best-effort; proceed to emit terminal event

                # Emit the deferred terminal event (or no terminal if stream ended without one)
                if deferred_terminal_item is not None:
                    await self._handle_stream_item(
                        deferred_terminal_item,
                        updater,
                        task,
                        streaming_artifact_id,
                        first_chunk_sent,
                        first_intermediate_chunk_sent,
                    )
                break  # Done — no re-invocation needed
        except asyncio.CancelledError:
            logger.info(f"Agent execution cancelled for context_id={context_id}")
            try:
                await asyncio.shield(
                    updater.update_status(
                        TaskState.canceled,
                        new_agent_text_message(
                            "Agent execution was cancelled.",
                            task.context_id,
                            task.id,
                        ),
                    )
                )
            except (asyncio.CancelledError, Exception):
                pass  # Best-effort: queue may already be closed
            raise
        except Exception as e:
            logger.error(f"An error occurred while streaming the response: {e.__class__.__name__}: {e}")

            # CRITICAL: Emit TaskState.failed before raising the exception
            # This ensures the orchestrator receives a proper failure status instead of
            # seeing the stream end abruptly with a "working" state
            error_message = f"Agent execution failed: {e.__class__.__name__}: {e}"
            try:
                await updater.update_status(
                    TaskState.failed,
                    new_agent_text_message(
                        error_message,
                        task.context_id,
                        task.id,
                    ),
                )
                logger.info(f"Emitted TaskState.failed to orchestrator: {error_message}")
            except Exception as emit_error:
                # If we can't emit the failure status, log but don't mask the original error
                logger.error(f"Failed to emit TaskState.failed: {emit_error}")

            raise ServerError(error=InternalError()) from e
        finally:
            # Log unconsumed steering messages before cleanup.
            # These arrive between the last abefore_model call and stream
            # completion — they will be picked up as a normal next turn.
            try:
                unconsumed = self.agent.get_pending_messages(context_id)
                if unconsumed:
                    logger.warning(
                        f"[STEERING] {len(unconsumed)} unconsumed steering message(s) "
                        f"for context_id={context_id} after execution finished. "
                        f"They will be handled as the next conversation turn."
                    )
            except Exception:
                pass  # Best-effort; don't mask the original result
            # Deregister active stream and clean up agent's message queue
            async with _active_streams_lock:
                _active_streams.pop(context_id, None)
            self.agent.clear_message_queue(context_id)

    async def _handle_stream_item(
        self,
        item,
        updater,
        task,
        streaming_artifact_id: str,
        first_chunk_sent: bool = False,
        first_intermediate_chunk_sent: bool = False,
    ) -> tuple[bool, bool]:
        """Handle a stream item from the agent and update the task accordingly.

        Streaming chunks (metadata.streaming_chunk=True) are emitted as
        TaskArtifactUpdateEvents with append=True, following the A2A protocol's
        recommended pattern for incremental content delivery.
        Status updates and terminal events use TaskStatusUpdateEvents.

        Args:
            item: AgentStreamResponse object from agent
            updater: TaskUpdater for sending updates
            task: Current task being processed
            streaming_artifact_id: Stable artifact ID for streaming chunks
            first_chunk_sent: Whether we've already sent the first main content chunk
            first_intermediate_chunk_sent: Whether we've already sent the first intermediate output chunk

        Returns:
            Tuple of (first_chunk_sent, first_intermediate_chunk_sent) flags
        """
        # item is an AgentStreamResponse object
        state = item.state
        content = item.content
        metadata = item.metadata or {}

        # --- Streaming content chunks → artifact-append ---
        if state == TaskState.working and metadata.get("streaming_chunk"):
            is_intermediate = metadata.get("intermediate_output", False)

            # Intermediate-output chunks (thinking/reasoning) go into a SEPARATE
            # artifact stream so they don't mix with the main response artifact.
            # Matches the orchestrator's executor pattern.
            if is_intermediate:
                effective_artifact_id = streaming_artifact_id + "-thought"
            else:
                effective_artifact_id = streaming_artifact_id

            # First chunk creates the artifact (append=False), subsequent chunks append (append=True)
            # Use separate tracking for intermediate output vs main content
            if is_intermediate:
                append = first_intermediate_chunk_sent
            else:
                append = first_chunk_sent

            await updater.add_artifact(
                [Part(root=TextPart(text=content))],
                artifact_id=effective_artifact_id,
                append=append,  # False for first chunk, True for subsequent
                last_chunk=False,
                metadata={"streaming_chunk": True},
            )
            # Update the appropriate tracking flag
            if is_intermediate:
                return (first_chunk_sent, True)  # Mark intermediate chunk sent
            return (True, first_intermediate_chunk_sent)  # Mark main chunk sent

        # Handle different A2A task states
        if state == TaskState.working:
            # Status update or intermediate progress
            logger.info(f"Emitting status update: {content}")
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
                metadata=metadata or None,
            )

        elif state == TaskState.failed:
            # Handle failure state (terminal state - stream will close)
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
            )

        elif state == TaskState.input_required:
            # User input required - leave task in input_required state
            await updater.update_status(
                TaskState.input_required,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
            )

        elif state == TaskState.auth_required:
            # Authentication required - leave task in auth_required state
            await updater.update_status(
                TaskState.auth_required,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
            )

        elif state == TaskState.completed:
            # Task completed successfully
            # If we've been streaming chunks, don't create a new artifact - content already streamed
            # Just complete the task; the streaming artifact contains all the content
            if not first_chunk_sent:
                # Only create artifact if we haven't been streaming
                await updater.add_artifact(
                    [Part(root=TextPart(text=content))],
                    name="agent_result",
                )
            # Always include final content in completion message so downstream
            # consumers (e.g. orchestrator) can extract the full response from
            # task.status.message even when content was streamed via artifacts.
            # TODO: is this duplication necessary, or can we rely on the artifact content alone for completed tasks?
            await updater.complete(
                message=new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                )
                if content
                else None,
            )

        else:
            # Unknown state - log warning and treat as completed
            logger.warning(f"Unknown task state: {state}, treating as completed")
            await updater.add_artifact(
                [Part(root=TextPart(text=content))],
                name="agent_result",
            )
            await updater.complete()

        # Return flags unchanged for non-streaming paths
        return (first_chunk_sent, first_intermediate_chunk_sent)

    def _validate_request(self, context: RequestContext) -> bool:
        """Validate the request context.

        Args:
            context: Request context to validate

        Returns:
            True if validation fails, False if validation passes
        """
        return False

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handle task cancellation.

        Emits a canceled status event so that DefaultRequestHandler.on_cancel_task()
        can complete the protocol flow.  The handler then cancels the producer_task
        asyncio.Task, which propagates CancelledError through the agent's stream.

        Args:
            context: Request context (must have task_id and context_id)
            event_queue: Event queue for publishing the cancel acknowledgment
        """
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        logger.info("Cancel requested for task_id=%s context_id=%s", task_id, context_id)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.canceled,
                    message=new_agent_text_message(
                        "Agent execution was cancelled.",
                        context_id,
                        task_id,
                    ),
                ),
                final=True,
            )
        )
