import asyncio
import logging
import uuid
from typing import Literal

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    InvalidParamsError,
    Message,
    Part,
    Task,
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
from agent_common.a2a.client_runnable import A2AClientRunnable
from agent_common.models.base import ModelType
from pydantic import SecretStr
from ringier_a2a_sdk.cost_tracking.logger import set_request_access_token
from ringier_a2a_sdk.server.executor import (
    MAX_STEERING_QUEUE_DEPTH,
    MAX_STEERING_REINVOCATIONS,
    ActiveStreamInfo,
    _active_streams,
    _active_streams_lock,
)

from app.models.responses import AgentStreamResponse

from ..models.config import UserConfig
from .a2a_extensions import (
    ACTIVITY_LOG_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    WORK_PLAN_EXTENSION,
    new_activity_log_message,
    new_work_plan_message,
)

# from google.adk.sessions import InMemorySessionService
from .agent import OrchestratorDeepAgent
from .budget_guard import get_budget_guard
from .registry import RegistryService, User
from .steering_state import (
    get_all_active_subagent_dispatches,
    get_orchestrator_pending_messages,
    get_steering_queue,
    register_steering_queue,
    remove_steering_queue,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class OrchestratorDeepAgentExecutor(AgentExecutor):
    """OrchestratorDeepAgent Executor Example."""

    def __init__(self, cost_logger=None):
        # Read orchestrator thinking configuration from environment
        self.agent = OrchestratorDeepAgent(cost_logger=cost_logger)
        self.registry_service = RegistryService()

    async def _get_user_from_registry(
        self, sub: str, access_token: str | None = None, sub_agent_config_hash: str | None = None
    ) -> User:
        """Fetch user from registry using the provided sub.

        Args:
            sub: The user's sub (OIDC subject identifier)
            access_token: The user's access token for authenticated API calls
            sub_agent_config_hash: Optional config hash for console testing mode

        Returns:
            User object with all user-specific data

        Raises:
            ServerError: If user is not found in registry
        """
        user = await self.registry_service.get_user(
            sub, access_token=access_token, sub_agent_config_hash=sub_agent_config_hash
        )
        if not user:
            logger.error(f"[REGISTRY] User with sub {sub} not found in registry")
            raise ServerError(error=InvalidParamsError())
        return user

    async def _build_user_config(
        self,
        user: User,
        user_sub: str,
        user_token: str,
        user_name: str,
        user_email: str,
        user_groups: list[str],
        model_choice: ModelType | None,
        message_formatting: Literal["markdown", "slack", "plain"],
        slack_user_handle: str | None,
        sub_agent_config_hash: str | None,
        enable_thinking: bool | None = None,
        thinking_level: str | None = None,
    ) -> UserConfig:
        """Build complete UserConfig with all data and discovered capabilities.

        Args:
            user: User object from registry
            user_sub: OIDC subject from JWT
            user_token: User access token
            user_name: User's full name
            user_email: User's email
            user_groups: User's group memberships
            model_choice: Optional model preference
            message_formatting: Message formatting style
            slack_user_handle: Optional Slack user handle
            sub_agent_config_hash: Optional console mode config hash
            preferred_model: Optional preferred model from registry
            enable_thinking: Optional thinking configuration from client
            thinking_level: Optional thinking level from client

        Returns:
            UserConfig: Fully initialized with static data and discovered tools/agents
        """
        # Build base UserConfig with static data from registry and request context
        user_config = UserConfig(
            user_sub=user_sub,  # OIDC sub from JWT
            user_id=user.id,  # Stable database ID from registry
            access_token=SecretStr(user_token),
            name=user_name,
            email=user_email,
            groups=user_groups,  # Pass groups for authorization
            model=model_choice,
            message_formatting=message_formatting,
            slack_user_handle=slack_user_handle,
            sub_agent_config_hash=sub_agent_config_hash,
            language=user.language,
            custom_prompt=user.custom_prompt,
            local_subagents=user.local_subagents,
            agent_metadata=user.agent_metadata,
            tool_names=user.tool_names,
            accessible_catalog_ids=user.catalog_ids or None,
            enable_thinking=enable_thinking,
            thinking_level=thinking_level,
        )

        # Discover capabilities (tools and sub-agents)
        logger.debug(f"Discovering capabilities for user_sub: {user_config.user_sub}")
        sub_agents = await self.agent.agent_discovery_service.register_agents(
            agent_metadata=user_config.agent_metadata or {},
            token=user_config.access_token.get_secret_value(),
        )

        # Discover ALL tools (without whitelist)
        # The whitelist will be applied later in build_runtime_context for orchestrator binding
        # Server info is stored in tool.metadata["server_name"] by MultiServerMCPClient
        tools = await self.agent.tool_discovery_service.discover_tools(
            user_config.access_token.get_secret_value(),
            white_list=None,  # Don't filter here - GP agent needs access to all tools
        )
        logger.debug(f"Discovered {len(sub_agents)} sub-agents: {[agent['name'] for agent in sub_agents]}")
        logger.debug(f"Discovered {len(tools)} total tools (unfiltered)")

        # Update user_config with discovered data
        user_config.tools = tools
        user_config.sub_agents = sub_agents

        logger.debug(f"Built complete UserConfig with {len(user_config.sub_agents)} sub-agents")

        return user_config

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Execute the agent task, handling both new requests and resumption from interrupts.

        This method implements the resumption mechanism for LangGraph interrupts:

        1. For new requests: Execute the agent normally using agent.stream()
        2. For resumption: Detect auth completion patterns in the user message
        3. If resuming: Use Command(resume=query) to resume from interrupt
        4. The graph resumes from where interrupt() was called

        The resumption happens when:
        - The graph has pending interrupts
        - The user message contains auth completion patterns
        - The Command(resume=value) is streamed to the graph

        This allows seamless continuation after authentication without losing context.

        Authentication:
        - User identity is validated by OidcAuthMiddleware before this method is called
        - Only authenticated users with valid OIDC tokens can reach this point
        - User info is available in request.state.user (set by middleware) but not directly
          accessible here since A2A SDK abstracts the request layer
        """
        # Note: Authentication is enforced at the middleware layer
        # All requests reaching this method have already been authenticated
        logger.debug("Executing request from authenticated user")

        error = self._validate_request(context)
        if error:
            raise ServerError(error=InvalidParamsError())

        query = context.get_user_input()
        task = context.current_task
        logger.debug(f"Starting execution for query: {query}")
        logger.debug(f"Current task: {task}")
        if not task:
            task = new_task(context.message)  # type: ignore
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        context_id = task.context_id

        # Extract caller identity early for steering authorization
        caller_sub: str | None = None
        if context.call_context and hasattr(context.call_context, "state"):
            caller_sub = context.call_context.state.get("user_sub")
        # Extract caller's channel ID from message metadata (for multi-user Slack conversations)
        caller_channel_id: str | None = None
        if context.message and context.message.metadata and isinstance(context.message.metadata, dict):
            caller_channel_id = context.message.metadata.get("slackChannelId")

        # --- Continuous Interaction Turn: route to active stream if one exists ---
        async with _active_streams_lock:
            active = _active_streams.get(context_id)
            if active is not None:
                # Verify the caller belongs to this conversation.
                # Channel (Slack): check caller's channel matches the stream's assistant_id.
                # Personal: check caller's user_sub matches the stream owner.
                # If scope is not yet set (race: stream just registered), allow through.
                if active.scope == "channel":
                    if active.assistant_id and caller_channel_id != active.assistant_id:
                        logger.warning(
                            f"[STEERING] Rejected steering for context_id={context_id}: "
                            f"caller channel_id does not match stream assistant_id"
                        )
                        raise ServerError(error=InvalidParamsError())
                elif active.scope == "personal":
                    if active.owner_sub and caller_sub != active.owner_sub:
                        logger.warning(
                            f"[STEERING] Rejected steering for context_id={context_id}: "
                            f"caller_sub={caller_sub} does not match stream owner"
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
                    f"queuing message for running orchestrator (queue depth: {active.message_queue.qsize() + 1})"
                )
                active.message_queue.put_nowait(context.message)
                # Also put into orchestrator-local queue (read by SteeringMiddleware)
                orch_queue = get_steering_queue(context_id)
                if orch_queue is not None:
                    orch_queue.put_nowait(context.message)
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
        stream_info = ActiveStreamInfo(context_id=context_id, task_id=task.id, owner_sub=caller_sub)
        orch_queue: asyncio.Queue[Message] = asyncio.Queue()
        async with _active_streams_lock:
            _active_streams[context_id] = stream_info
        register_steering_queue(context_id, orch_queue)

        # ZERO-TRUST: Extract verified user_sub and token from call_context (set by RequestContextBuilder)
        if context.call_context and hasattr(context.call_context, "state"):
            try:
                user_sub = context.call_context.state["user_sub"]  # OIDC subject from JWT
                user_token = context.call_context.state["user_token"]
                user_name = context.call_context.state["user_name"]
                user_email = context.call_context.state["user_email"]
                user_groups = context.call_context.state.get("user_groups", [])
                # Optional: console mode sub-agent config hash for isolated testing
                sub_agent_config_hash = context.call_context.state.get("sub_agent_config_hash")
            except KeyError as e:
                logger.error(f"[ZERO-TRUST] Missing expected user context key: {e}")
                raise ServerError(error=InvalidParamsError()) from e
        else:
            logger.error("[ZERO-TRUST] No user_token found in call_context - authentication may have failed")
            raise ServerError(error=InvalidParamsError())

        # Set the access token for cost tracking (ContextVar)
        set_request_access_token(user_token)
        logger.info(f"[ZERO-TRUST] Using verified user_sub for graph retrieval: {user_sub}")
        if sub_agent_config_hash:
            logger.info(f"[CONSOLE] Console mode enabled for sub-agent config hash: {sub_agent_config_hash}")

        # Fetch user from registry to get stable database ID (user.id)
        # This allows us to use the database ID in config metadata instead of OIDC sub e.g. for docstore read/write
        user = await self._get_user_from_registry(
            user_sub,
            access_token=user_token,
            sub_agent_config_hash=sub_agent_config_hash,
        )
        logger.info(f"[REGISTRY] Retrieved user from registry: database_id={user.id}, sub={user.sub}")

        # Extract metadata from both message-level and params-level (message takes priority)
        logger.info(f"[EXECUTOR] Params-level metadata: {context.metadata}")
        logger.info(f"[EXECUTOR] Message-level metadata: {context.message.metadata if context.message else None}")
        message_metadata = context.message.metadata if context.message and context.message.metadata else {}
        params_metadata = context.metadata or {}

        # Merge metadata with message-level taking priority
        request_metadata = {**params_metadata, **message_metadata}

        model_choice = user.preferred_model or request_metadata.get("model")
        enable_thinking = user.enable_thinking or request_metadata.get("enableThinking") in ("true", "1", "yes")
        thinking_level = user.thinking_level or request_metadata.get("thinkingLevel") if enable_thinking else None
        logger.debug(
            f"[THINKING CONFIG] model_choice={model_choice}, enable_thinking={enable_thinking}, thinking_level={thinking_level}"
        )

        # Check budget guard before processing request
        budget_guard = get_budget_guard()
        if budget_guard and budget_guard.enabled and budget_guard.is_locked:
            status = budget_guard.get_status()
            logger.warning(
                f"Request rejected due to budget lock. "
                f"Usage: {status.current_usage:,}/{status.token_limit:,} tokens. "
                f"Reason: {status.lock_reason}"
            )
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(
                    "Service temporarily unavailable: Monthly token budget has been exceeded. "
                    "Please contact an administrator to increase the budget or wait until next month.",
                    task.context_id,
                    task.id,
                ),
            )
            return

        try:
            # Extract slack user handle - support both naming conventions
            # Client may send 'slackUserId' (camelCase) or 'slack_user_id' (snake_case)
            slack_user_id = request_metadata.get("slackUserId")
            slack_channel_id = request_metadata.get("slackChannelId")  # for filesystem namespace isolation

            # Update stream info with scope and assistant_id now that we have them
            stream_info.scope = "channel" if slack_channel_id else "personal"
            stream_info.assistant_id = slack_channel_id if slack_channel_id else str(user.id)

            if slack_user_id:
                slack_user_handle = f"<@{slack_user_id}>"
            else:
                slack_user_handle = None

            # Extract message formatting - support both naming conventions
            message_formatting = (
                request_metadata.get("messageFormatting") or request_metadata.get("message_formatting") or "markdown"
            )

            # Build complete UserConfig with all data and discovered capabilities
            user_config = await self._build_user_config(
                user=user,
                user_sub=user_sub,
                user_token=user_token,
                user_name=user_name,
                user_email=user_email,
                user_groups=user_groups,
                model_choice=model_choice,
                message_formatting=message_formatting,
                slack_user_handle=slack_user_handle,
                sub_agent_config_hash=sub_agent_config_hash,
                enable_thinking=enable_thinking,
                thinking_level=thinking_level,
            )

            # Extract message parts for multimodal support (text + files)
            message_parts = context.message.parts if context.message else []

            # Check if we need to resume from an interrupt
            # Get or create graph for this user's configuration
            # ZERO-TRUST: Pass verified user_sub and user_token from call_context
            if user_config.enable_thinking is False:
                thinking_level = None
            elif user_config.enable_thinking is True and not user_config.thinking_level:
                thinking_level = "low"  # Default to low if enabled but not specified
            elif user_config.enable_thinking is True and user_config.thinking_level:
                thinking_level = user_config.thinking_level
            elif user_config.enable_thinking is None:
                thinking_level = self.agent._default_thinking_level

            model_type = user_config.model if user_config.model else self.agent._default_model_type
            graph = await self.agent.get_or_create_graph(
                model_type=model_type,
                thinking_level=thinking_level,
            )

            # NOTE: we decide to use channel_id as part of the filesystem namespace since if one has access to the
            # channel, she should have access to all files shared in that channel.
            # This is a design decision based on Slack's permission model.
            # Create config for graph execution with interrupt support
            # CRITICAL: Include __pregel_checkpointer to prevent LangGraph from misinterpreting checkpoint_ns as subgraph
            config = {
                "configurable": {
                    "thread_id": task.context_id,
                    "__pregel_checkpointer": graph.checkpointer,  # Required for proper checkpoint isolation
                },
                "metadata": {
                    "assistant_id": slack_channel_id
                    if slack_channel_id
                    else user.id,  # Use database ID (not OIDC sub) to match docstore tools
                    "user_id": user.id,  # Stable database ID (not OIDC sub)
                    "conversation_id": task.context_id,  # For conversation-scoped tool result storage
                    "user_name": user_name,
                    "slack_thread_ts": request_metadata.get("slackThreadTs"),
                    "scope": "personal" if not slack_channel_id else "channel",
                    "model_type": model_type,
                    "thinking_level": thinking_level,
                },
                "tags": [
                    f"user_sub:{user_sub}",  # Keep OIDC sub in tags for tracing
                    f"user_id:{user.id}",  # Add database ID tag
                    f"conversation:{task.context_id}",
                ],
            }

            current_state = graph.get_state(config)  # type: ignore

            # Check if the graph is currently interrupted and this might be a resume request
            resume_value = None  # Initialize resume_value
            if hasattr(current_state, "interrupts") and current_state.interrupts:
                # Parse interrupt type and handle permission grants
                last_interrupt = current_state.interrupts[-1]
                interrupt_value = getattr(last_interrupt, "value", last_interrupt)
                interrupt_type = interrupt_value.get("type") if isinstance(interrupt_value, dict) else None

                if interrupt_type in (
                    "file_permission_request",
                    "search_permission_request",
                    "bulk_file_permission_request",
                ):
                    # Permission-related interrupt - handle approval/denial
                    logger.info(f"Resuming from permission interrupt: {interrupt_type}")

                    # Parse user response
                    response = query.lower().strip() if query else ""

                    if interrupt_type == "file_permission_request":
                        # TODO: this logic is too basic, improve with NLP parsing if needed
                        # Single file permission
                        file_path = interrupt_value.get("file_path") if isinstance(interrupt_value, dict) else None
                        if response in ("yes", "approve", "allow", "grant"):
                            resume_value = "approve"
                            # Update state to grant permission
                            state_update = current_state.values.copy() if hasattr(current_state, "values") else {}
                            granted_files = state_update.get("personal_file_read_permissions", set())
                            if not isinstance(granted_files, set):
                                granted_files = set(granted_files) if granted_files else set()
                            granted_files.add(file_path)
                            state_update["personal_file_read_permissions"] = granted_files
                            # State will be updated by the graph when resumed
                            logger.info(f"Granted permission for file: {file_path}")
                        else:
                            resume_value = "deny"
                            logger.info(f"Denied permission for file: {file_path}")

                    elif interrupt_type == "search_permission_request":
                        # Personal search permission
                        if response in ("yes", "approve", "allow", "grant"):
                            resume_value = "approve"
                            # Update state to grant search permission
                            state_update = current_state.values.copy() if hasattr(current_state, "values") else {}
                            state_update["personal_search_permission"] = True
                            logger.info("Granted personal search permission")
                        else:
                            resume_value = "deny"
                            logger.info("Denied personal search permission")

                    elif interrupt_type == "bulk_file_permission_request":
                        # Bulk file permission
                        if response in ("approve all", "approve_all", "yes all", "grant all"):
                            resume_value = "approve_all"
                            logger.info("Bulk approval: approve all files")
                        elif response in ("deny all", "deny_all", "no all"):
                            resume_value = "deny_all"
                            logger.info("Bulk approval: deny all files")
                        elif response in ("review", "individual", "one by one"):
                            resume_value = "review"
                            logger.info("Bulk approval: review individually")
                        else:
                            # Default to deny for safety
                            resume_value = "deny_all"
                            logger.info(f"Unknown bulk response '{response}', defaulting to deny_all")
                else:
                    # Other interrupt types (auth, etc.)
                    resume_value = query
                    logger.info(f"Resuming from interrupt: {interrupt_type or 'unknown'}")

            if resume_value is None:
                logger.info("Normal execution (not resuming from interrupt)")

            # Extension activation: client must send X-A2A-Extensions header to enable
            # extensions. No header = extensions disabled (per A2A spec).
            requested_extensions: set[str] | None = None
            if context.call_context and hasattr(context.call_context, "requested_extensions"):
                requested_extensions = context.call_context.requested_extensions
            if requested_extensions is not None:
                logger.info(f"[EXTENSIONS] Client requested extensions: {requested_extensions}")
            else:
                logger.info("[EXTENSIONS] No extensions requested (header absent)")

            # emit a started status update as part of activity log
            if requested_extensions is not None and ACTIVITY_LOG_EXTENSION in requested_extensions:
                logger.debug("Agent execution started. Emitting initial activity log message.")
                await updater.update_status(
                    TaskState.working,
                    new_activity_log_message(
                        "Agent execution started.",
                        task.context_id,
                        task.id,
                    ),
                )
                logger.debug("Initial activity log message emitted.")

            # Stable artifact ID for streaming content chunks (A2A artifact-append pattern)
            steering_reinvocations = 0

            while True:
                streaming_artifact_id = str(uuid.uuid4())
                first_chunk_sent = False  # Track if we've sent the initial artifact
                deferred_terminal_item = None

                async for item in self.agent.stream(message_parts, user_config, config=config, resume=resume_value):
                    # Buffer the terminal completed item so we can check for unconsumed
                    # steering messages before emitting it to the SSE stream.
                    # Other terminal states are emitted immediately.
                    if item.state == TaskState.completed:
                        deferred_terminal_item = item
                        continue

                    # Skip costly graph.get_state() for transient streaming chunks
                    metadata = item.metadata or {}
                    if metadata.get("streaming_chunk"):
                        is_final = True  # Doesn't matter for streaming chunks
                    else:
                        current_state = graph.get_state(config)  # type: ignore
                        if hasattr(current_state, "interrupts") and current_state.interrupts:
                            is_final = False
                        else:
                            is_final = True

                    # Pass first_chunk_sent flag and update it after each chunk
                    first_chunk_sent = await self._handle_stream_item(
                        item,
                        updater,
                        task,
                        is_final=is_final,
                        streaming_artifact_id=streaming_artifact_id,
                        first_chunk_sent=first_chunk_sent,
                        active_extensions=requested_extensions,
                    )

                # Check for steering messages that arrived after the last abefore_model
                if (
                    deferred_terminal_item is not None
                    and deferred_terminal_item.state == TaskState.completed
                    and steering_reinvocations < MAX_STEERING_REINVOCATIONS
                ):
                    unconsumed = get_orchestrator_pending_messages(context_id)
                    if unconsumed:
                        # Build new message parts from the unconsumed steering messages
                        new_parts: list[Part] = []
                        for msg in unconsumed:
                            if msg.parts:
                                new_parts.extend(msg.parts)

                        if new_parts:
                            steering_reinvocations += 1
                            message_parts = new_parts
                            resume_value = None  # Not resuming — fresh turn
                            logger.info(
                                f"[STEERING] Re-invoking orchestrator with {len(unconsumed)} late steering "
                                f"message(s) (reinvocation {steering_reinvocations}/{MAX_STEERING_REINVOCATIONS})"
                            )
                            continue  # Loop back for another stream round

                # Emit the deferred terminal event
                if deferred_terminal_item is not None:
                    metadata = deferred_terminal_item.metadata or {}
                    if metadata.get("streaming_chunk"):
                        is_final = True
                    else:
                        current_state = graph.get_state(config)  # type: ignore
                        if hasattr(current_state, "interrupts") and current_state.interrupts:
                            is_final = False
                        else:
                            is_final = True
                    await self._handle_stream_item(
                        deferred_terminal_item,
                        updater,
                        task,
                        is_final=is_final,
                        streaming_artifact_id=streaming_artifact_id,
                        first_chunk_sent=first_chunk_sent,
                        active_extensions=requested_extensions,
                    )
                break  # Done — no re-invocation needed
        except asyncio.CancelledError:
            logger.info(f"Orchestrator execution cancelled for context_id={context_id}")
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
            logger.error(f"An error occurred while streaming the response: {e.__class__.__name__}: {e}", exc_info=True)
            raise ServerError(error=InternalError()) from e
        finally:
            # Log unconsumed steering messages before cleanup.
            # These arrive between the last abefore_model call and stream
            # completion — they will be handled as the next conversation turn.
            unconsumed = get_orchestrator_pending_messages(context_id)
            if unconsumed:
                logger.warning(
                    f"[STEERING] {len(unconsumed)} unconsumed steering message(s) "
                    f"for context_id={context_id} after execution finished. "
                    f"They will be handled as the next conversation turn."
                )
            # Deregister active stream and clean up orchestrator steering queue
            async with _active_streams_lock:
                _active_streams.pop(context_id, None)
            remove_steering_queue(context_id)

    async def _handle_stream_item(
        self,
        item: AgentStreamResponse,
        updater: TaskUpdater,
        task: Task,
        is_final: bool,
        streaming_artifact_id: str = "",
        first_chunk_sent: bool = False,
        active_extensions: set[str] | None = None,
    ) -> bool:
        """Handle a stream item from the agent and update the task accordingly.

        Streaming chunks (metadata.streaming_chunk=True) are emitted as
        TaskArtifactUpdateEvents with append=True for ALL chunks (including first).
        The backend accumulates all artifact-update messages into a single response.
        Status updates and terminal events use TaskStatusUpdateEvents.

        Args:
            item: The stream response item
            updater: Task updater for sending events
            task: Current task
            is_final: Whether this is the final state
            streaming_artifact_id: Stable artifact ID for streaming chunks
            first_chunk_sent: Whether we've already sent the first chunk

        Returns:
            Updated first_chunk_sent flag
        """
        # item is an AgentStreamResponse object
        state = item.state
        content = item.content
        metadata = item.metadata or {}

        # Extension activation: helper to check if an extension should be emitted.
        # None means no header was sent → all extensions disabled (per A2A spec).
        def _ext_active(uri: str) -> bool:
            return active_extensions is not None and uri in active_extensions

        # --- Activity log items (tool calls, delegations) → status-update with extension ---
        # Must be handled BEFORE streaming_chunk check.
        if metadata.get("activity_log"):
            if not _ext_active(ACTIVITY_LOG_EXTENSION):
                return first_chunk_sent  # Client didn't request this extension
            source = metadata.get("source")
            logger.info(f"[ACTIVITY_LOG] Emitting status update: source={source}, content: {content[:50]}")
            await updater.update_status(
                TaskState.working,
                new_activity_log_message(
                    content,
                    task.context_id,
                    task.id,
                    source=source,
                ),
            )
            return first_chunk_sent  # Don't modify first_chunk_sent flag

        # --- Work plan items (todo snapshots) → status-update with DataPart extension ---
        if metadata.get("work_plan"):
            if not _ext_active(WORK_PLAN_EXTENSION):
                return first_chunk_sent  # Client didn't request this extension
            todos = metadata.get("todos", [])
            logger.info(f"[WORK_PLAN] Emitting work plan with {len(todos)} todos")
            await updater.update_status(
                TaskState.working,
                new_work_plan_message(
                    todos,
                    task.context_id,
                    task.id,
                ),
            )
            return first_chunk_sent  # Don't modify first_chunk_sent flag

        # --- Streaming content chunks → artifact-append ---
        # NOTE: Using proper A2A artifact-append protocol
        # Known limitation: The A2A client library may buffer artifact-update events
        # until the next status-update event arrives (SSE buffering in the Python client)
        # Streaming chunks will appear with slight delay until next natural status update
        # (which happens frequently during LLM token streaming)
        if state == TaskState.working and metadata.get("streaming_chunk"):
            # Intermediate-output chunks (sub-agent thoughts, orchestrator reasoning) go into
            # a SEPARATE artifact stream so they don't mix with the main response artifact.
            # Main response chunks use streaming_artifact_id; thoughts use a "-thought" suffix.
            # This also ensures first_chunk_sent correctly reflects MAIN CONTENT only, so that
            # include_subagent_output=True can still produce a non-streaming final response.
            if metadata.get("intermediate_output"):
                effective_artifact_id = streaming_artifact_id + "-thought"
            else:
                effective_artifact_id = streaming_artifact_id

            append = True  # Always True for all chunks (backend accumulates all artifact-update messages)
            logger.info(
                f"[STREAMING] Calling add_artifact: len={len(content)}, append={append}, artifact_id={effective_artifact_id}"
            )
            # Determine artifact extensions for intermediate output (sub-agent thoughts)
            artifact_extensions = [INTERMEDIATE_OUTPUT_EXTENSION] if metadata.get("intermediate_output") else None
            # If intermediate output extension isn't active, suppress entirely (don't leak reasoning to clients)
            if artifact_extensions and not _ext_active(INTERMEDIATE_OUTPUT_EXTENSION):
                return first_chunk_sent
            # Keep agent_name in artifact metadata for attribution
            artifact_metadata = {}
            if metadata.get("agent_name"):
                artifact_metadata["agent_name"] = metadata["agent_name"]
            await updater.add_artifact(
                [Part(root=TextPart(text=content))],
                artifact_id=effective_artifact_id,
                append=append,
                last_chunk=False,
                metadata=artifact_metadata or {},
                extensions=artifact_extensions,
            )
            logger.info("[STREAMING] Artifact chunk enqueued")
            # Intermediate-output chunks are supplementary (sub-agent thinking).
            # They must NOT claim first_chunk_sent — the main response comes later
            # either as regular orchestrator streaming tokens or via include_subagent_output.
            # If we set first_chunk_sent=True here the executor would skip emitting the
            # include_subagent_output content and the final answer would never reach the user.
            if metadata.get("intermediate_output"):
                return first_chunk_sent
            return True  # Mark that first chunk has been sent

        # Handle different A2A task states
        if state == TaskState.working and not is_final:
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

        elif state == TaskState.working and is_final:
            # Working state with no pending interrupts - still working
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

        elif state == TaskState.completed and is_final:
            # Task completed successfully
            if first_chunk_sent:
                # We streamed ORCHESTRATOR token chunks (not sub-agent thoughts)
                # Close the artifact stream and send completion status without message.
                # The streamed chunks already contain the complete response.
                logger.info("[STREAMING] Closing artifact stream (orchestrator content already streamed)")
                await updater.add_artifact(
                    [Part(root=TextPart(text=""))],
                    artifact_id=streaming_artifact_id,
                    append=True,
                    last_chunk=True,
                    metadata={},
                )
                await updater.update_status(
                    TaskState.completed,
                    metadata=metadata or None,
                )
            else:
                # Non-streaming completion: include content in the status message.
                # This is the orchestrator's FINAL ANSWER - always send it.
                # Sub-agent outputs were intermediate thoughts; this is authoritative.
                await updater.update_status(
                    TaskState.completed,
                    new_agent_text_message(
                        content if content else "Task completed successfully",
                        task.context_id,
                        task.id,
                    ),
                    metadata=metadata or None,
                )

        elif state == TaskState.completed and not is_final:
            logger.info(f"Contradictory completed non-final state, treating as input_required: {content}")
            # User input required - leave task in input_required state
            await updater.update_status(
                TaskState.input_required,
                new_agent_text_message(
                    content,
                    task.context_id,
                    task.id,
                ),
            )
        else:
            # Unknown state - log warning and treat as completed
            logger.warning(f"Unknown task state: {state}, treating as completed")
            await updater.add_artifact(
                [Part(root=TextPart(text=content))],
                name="orchestrator_result",
            )
            await updater.complete()

        # Return first_chunk_sent unchanged for non-streaming paths
        return first_chunk_sent

    def _validate_request(self, context: RequestContext) -> bool:
        return False

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handle task cancellation with sub-agent propagation.

        Propagates cancel to all active sub-agents (if any) via A2A tasks/cancel,
        then emits a canceled status event.  DefaultRequestHandler.on_cancel_task()
        subsequently cancels the producer_task asyncio.Task.
        """
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        logger.info("Cancel requested for orchestrator task_id=%s context_id=%s", task_id, context_id)

        # Propagate cancel to all active sub-agents (best-effort, in parallel)
        dispatches = get_all_active_subagent_dispatches(context_id)
        if dispatches:
            cancel_coros = []
            for dispatch in dispatches:
                if dispatch.subagent_task_id and isinstance(dispatch.runnable, A2AClientRunnable):
                    logger.info(
                        "Propagating cancel to sub-agent %s (task_id=%s)",
                        dispatch.subagent_name,
                        dispatch.subagent_task_id,
                    )
                    cancel_coros.append(dispatch.runnable.cancel_task(dispatch.subagent_task_id))
            if cancel_coros:
                results = await asyncio.gather(*cancel_coros, return_exceptions=True)
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        logger.warning(
                            "Failed to propagate cancel to a sub-agent: %s",
                            result,
                        )

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
