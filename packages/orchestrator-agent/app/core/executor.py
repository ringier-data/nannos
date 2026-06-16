import asyncio
import logging
import os
import uuid
from typing import Any, Literal

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.helpers import (
    new_task_from_user_message,
    new_text_message,
)
from a2a.types import (
    InternalError,
    InvalidParamsError,
    Message,
    Part,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
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
    FEEDBACK_REQUEST_EXTENSION,
    HUMAN_IN_THE_LOOP_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    WORK_PLAN_EXTENSION,
    new_activity_log_message,
    new_feedback_request_message,
    new_hitl_interrupt_message,
    new_work_plan_message,
)

# from google.adk.sessions import InMemorySessionService
from ..handlers import StreamHandler
from .agent import OrchestratorDeepAgent
from .budget_guard import get_budget_guard
from .registry import RegistryService, User
from .turn_state import TurnState, count_tool_messages
from .steering_state import (
    get_all_active_subagent_dispatches,
    get_orchestrator_pending_messages,
    get_steering_queue,
    register_steering_queue,
    remove_steering_queue,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bounded re-entries to recover from an "eager completion" where the model sets
# include_subagent_output=true but never actually delegated (no `task` call).
MAX_DELEGATION_REINVOCATIONS = int(os.getenv("MAX_DELEGATION_REINVOCATIONS", "1"))

# Corrective nudge fed back into the graph on such a re-entry.
_DELEGATION_NUDGE = (
    "Your previous response set include_subagent_output=true but you did not call the `task` tool, "
    "so no sub-agent was actually invoked and there is no sub-agent output to include. "
    "Do not finalize yet. Call the `task` tool now to delegate the work to the appropriate sub-agent, "
    "then base your final response on the sub-agent's result."
)


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
            raise InvalidParamsError()
        return user

    @staticmethod
    def _extract_hitl_decisions(context: RequestContext) -> dict:
        """Extract HITL decisions from the incoming A2A message DataPart.

        Clients send decisions as a DataPart with {"decisions": [...]}.

        Returns:
            A dict like {"decisions": [{"type": "approve"}]}
        """
        from google.protobuf.json_format import MessageToDict

        if context.message and context.message.parts:
            for part in context.message.parts:
                if part.WhichOneof("content") == "data":
                    data = MessageToDict(part.data)
                    if isinstance(data, dict) and "decisions" in data:
                        return data

        logger.warning("[HITL] No data part with decisions found, defaulting to reject")
        return {"decisions": [{"type": "reject"}]}

    @staticmethod
    def _action_request_call_id(action_request: Any) -> Any:
        """Extract the stable per-call id an action_request carries, if any.

        Attached as top-level ``args._call_id`` by the outbound HITL builders (PTC
        ``_build_ptc_hitl_request`` and ConditionalHumanInTheLoopMiddleware) for EVERY
        interrupted call — static guard, risk-scored, or PTC eval. Returns ``None`` for
        action_requests that predate / don't carry it.
        """
        if not isinstance(action_request, dict):
            return None
        return (action_request.get("args") or {}).get("_call_id")

    @classmethod
    def _decisions_for_interrupt(cls, action_requests: list, hitl_decisions: list, decisions_by_id: dict) -> list:
        """Resolve the decision list for ONE interrupt, aligned to its action_requests.

        - By id (new clients): when the client sent id-keyed decisions, align each
          action_request to its own decision by ``call_id``. Robust to client ordering,
          to model replay reordering, and to a flat by-id list spanning multiple
          co-pending interrupts. Any call whose decision is missing (stale/absent
          ``call_id``) defaults to a safe reject — the returned list is therefore always
          exactly ``len(action_requests)`` long, so a partial payload can never crash the
          downstream count check nor silently auto-approve an unanswered call.
        - Blanket (legacy clients): a single decision is replicated to the
          action_request count. Anything else passes through unchanged.
        """
        n = len(action_requests)
        if n > 0 and decisions_by_id:
            call_ids = [cls._action_request_call_id(ar) for ar in action_requests]
            return [decisions_by_id.get(cid, {"type": "reject"}) for cid in call_ids]
        if len(hitl_decisions) == 1 and n > 1:
            return hitl_decisions * n
        return hitl_decisions

    @classmethod
    def _build_interrupt_resume_map(cls, interrupts: Any, hitl_decisions: list, query: Any) -> dict[str, Any]:
        """Build an interrupt-id-keyed resume map for ``Command(resume=...)``.

        LangGraph >=1.2 requires an id-keyed map whenever more than one interrupt is
        pending (e.g. two parallel ``task`` dispatches that each surfaced a sub-agent
        HITL) — a bare ``Command(resume=value)`` raises RuntimeError in that case.
        ``Interrupt.id`` is the xxh3 namespace hash the runtime matches the map against
        (``types.Interrupt.from_ns`` <-> ``pregel/_algo._scratchpad``), so keying by
        ``intr.id`` is correct for 1 *or* N pending interrupts.

        Decisions are aligned to each interrupt's ``action_requests`` by per-call id
        when the client sends them (one decision per call), else the single blanket
        decision is replicated. A flat by-id decision list is self-routing across both
        levels of multiplicity — interrupts (by ``intr.id``) and action_requests within
        an interrupt (by ``call_id``). Non-HITL interrupts (auth, etc.) resume with the
        raw ``query``.
        """
        decisions_by_id = {d["id"]: d for d in hitl_decisions if isinstance(d, dict) and "id" in d}
        resume_map: dict[str, Any] = {}
        for intr in interrupts:
            intr_value = getattr(intr, "value", intr)
            if isinstance(intr_value, dict) and "action_requests" in intr_value:
                action_requests = intr_value.get("action_requests", [])
                per = cls._decisions_for_interrupt(action_requests, hitl_decisions, decisions_by_id)
                resume_map[intr.id] = {"decisions": per}
                tool_names = [ar.get("name") for ar in action_requests if isinstance(ar, dict)]
                logger.info(f"Resuming HITL interrupt {intr.id} for tools {tool_names} with {len(per)} decision(s)")
            else:
                resume_map[intr.id] = query
                logger.info(f"Resuming non-HITL interrupt {intr.id}")
        return resume_map

    async def _build_user_config(
        self,
        user: User,
        user_sub: str,
        user_token: str,
        user_name: str,
        user_email: str,
        user_groups: list[str],
        model_choice: ModelType | None,
        message_formatting: Literal["markdown", "slack", "google-chat", "plain"],
        client_user_handle: str | None,
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
            client_user_handle: Optional client user handle for @-mentions (Slack: <@U123>, Google Chat: <users/123>)
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
            client_user_handle=client_user_handle,
            sub_agent_config_hash=sub_agent_config_hash,
            language=user.language,
            custom_prompt=user.custom_prompt,
            local_subagents=user.local_subagents,
            agent_metadata=user.agent_metadata,
            tool_names=user.tool_names,
            accessible_catalog_ids=user.catalog_ids or None,
            enable_thinking=enable_thinking,
            thinking_level=thinking_level,
            user_system_role=user.system_role,
            tool_bypass_rules=user.tool_bypass_rules,
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
            raise InvalidParamsError()

        query = context.get_user_input()
        task = context.current_task
        logger.debug(f"Starting execution for query: {query}")
        logger.debug(f"Current task: {task}")
        if not task:
            task = new_task_from_user_message(context.message)  # type: ignore
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        context_id = task.context_id

        # Extract caller identity early for steering authorization
        caller_sub: str | None = None
        if context.call_context and hasattr(context.call_context, "state"):
            caller_sub = context.call_context.state.get("user_sub")
        # Extract caller's channel ID from message metadata (for multi-user conversations)
        caller_channel_id: str | None = None
        if context.message and context.message.metadata and isinstance(context.message.metadata, dict):
            caller_channel_id = context.message.metadata.get("slackChannelId") or context.message.metadata.get(
                "googleChatSpaceId"
            )

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
                        raise InvalidParamsError()
                elif active.scope == "personal":
                    if active.owner_sub and caller_sub != active.owner_sub:
                        logger.warning(
                            f"[STEERING] Rejected steering for context_id={context_id}: "
                            f"caller_sub={caller_sub} does not match stream owner"
                        )
                        raise InvalidParamsError()
                if active.message_queue.qsize() >= MAX_STEERING_QUEUE_DEPTH:
                    logger.warning(
                        f"[STEERING] Queue full for context_id={context_id} "
                        f"(depth={active.message_queue.qsize()}), rejecting"
                    )
                    raise InvalidParamsError()
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
                raise InvalidParamsError() from e
        else:
            logger.error("[ZERO-TRUST] No user_token found in call_context - authentication may have failed")
            raise InvalidParamsError()

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
                TaskState.TASK_STATE_FAILED,
                new_text_message("Service temporarily unavailable: Monthly token budget has been exceeded. "
                    "Please contact an administrator to increase the budget or wait until next month.", context_id=task.context_id, task_id=task.id),
            )
            return

        user_config = None
        try:
            # Extract client user handle - support both Slack and Google Chat
            # Client may send 'slackUserId' (camelCase) or 'slack_user_id' (snake_case)
            slack_user_id = request_metadata.get("slackUserId")
            slack_channel_id = request_metadata.get("slackChannelId")  # for filesystem namespace isolation

            # Google Chat metadata
            google_chat_user_id = request_metadata.get("googleChatUserId")
            google_chat_space_id = request_metadata.get("googleChatSpaceId")

            # Determine channel_id from either Slack or Google Chat
            channel_id = slack_channel_id or google_chat_space_id

            # Update stream info with scope and assistant_id now that we have them
            # Slack: Public channels (which start with C) or private channels/group DMs (which start with G), a 1:1 direct message channel ID always starts with a D (e.g., D12345678).
            # Google-chat: in the google-chat client we set a spaceId just whenever source != direct_message.
            if (slack_channel_id and not slack_channel_id.startswith("D")) or google_chat_space_id:
                stream_info.scope = "channel"
                stream_info.assistant_id = channel_id
            else:
                stream_info.scope = "personal"
                # Use database ID (not OIDC sub) to match docstore tools
                stream_info.assistant_id = str(user.id)

            if slack_user_id:
                client_user_handle = f"<@{slack_user_id}>"
            elif google_chat_user_id:
                client_user_handle = f"<{google_chat_user_id}>"
            else:
                client_user_handle = None

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
                client_user_handle=client_user_handle,
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
                    "assistant_id": stream_info.assistant_id,
                    "user_id": user.id,  # Stable database ID (not OIDC sub)
                    "conversation_id": task.context_id,  # For conversation-scoped tool result storage
                    "group_id": user_groups[0] if user_groups else None,  # Primary group for filesystem namespace
                    "group_ids": user_groups or None,  # All groups for playbook aggregation
                    "user_name": user_name,
                    "slack_thread_ts": request_metadata.get("slackThreadTs"),
                    "google_chat_thread_id": request_metadata.get("googleChatThreadId"),
                    "scope": stream_info.scope,
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
                # LangGraph >=1.2 requires an interrupt-id-keyed resume map whenever
                # more than one interrupt is pending (e.g. two parallel `task`
                # dispatches that each surfaced a sub-agent HITL). A bare
                # Command(resume=value) raises RuntimeError in that case
                # (pregel/_loop.py: "you must specify the interrupt id when resuming").
                # Interrupt.id IS the xxh3 namespace hash the runtime matches against
                # (types.Interrupt.from_ns <-> pregel/_algo._scratchpad), so keying by
                # intr.id is correct for 1 *or* N pending interrupts.
                #
                # NOTE: this applies the same blanket decision to every co-pending
                # interrupt (matching today's single approve/reject UI). Per-interrupt
                # decisions would require the client to key decisions by interrupt id.
                # Clients send decisions as a DataPart (structured JSON, no XML).
                hitl_decisions = self._extract_hitl_decisions(context).get("decisions", [])
                resume_value = self._build_interrupt_resume_map(current_state.interrupts, hitl_decisions, query)

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
                    TaskState.TASK_STATE_WORKING,
                    new_activity_log_message(
                        "Agent execution started.",
                        task.context_id,
                        task.id,
                    ),
                )
                logger.debug("Initial activity log message emitted.")

            # Stable artifact ID for streaming content chunks (A2A artifact-append pattern)
            steering_reinvocations = 0
            delegation_reinvocations = 0

            while True:
                streaming_artifact_id = str(uuid.uuid4())
                first_chunk_sent = False  # Track if we've sent the initial MAIN artifact chunk
                first_intermediate_chunk_sent = False  # Track if we've sent the initial INTERMEDIATE artifact chunk
                streamed_chars = 0  # Total chars streamed via the MAIN artifact (code points, not wire bytes; for completion diagnostics)
                deferred_terminal_item = None
                # Per-round carrier: the agent populates this from its single
                # end-of-stream aget_state, so the phantom / feedback / terminal
                # checks below reuse it instead of re-reading the checkpoint
                # (~1.5–5s each). Must be a per-round local — agent/executor are
                # shared singletons.
                turn_state = TurnState()

                async for item in self.agent.stream(
                    message_parts, user_config, config=config, resume=resume_value, turn_state=turn_state
                ):
                    # Buffer the terminal completed item so we can check for unconsumed
                    # steering messages before emitting it to the SSE stream.
                    # Other terminal states are emitted immediately.
                    if item.state == TaskState.TASK_STATE_COMPLETED:
                        deferred_terminal_item = item
                        continue

                    metadata = item.metadata or {}
                    # is_final for in-loop items is only consumed by plain WORKING status
                    # items (960/969), which carry metadata=None — so the two branches are
                    # identical. activity_log / work_plan / streaming_chunk items return
                    # before then, and interrupt items don't read it. So is_final is
                    # immaterial here; use a constant and skip the per-item checkpoint read.
                    # (Validated via the shadow phase: see git history.)
                    is_final = True
                    if metadata.get("streaming_chunk"):
                        # Track MAIN-artifact bytes only (intermediate sub-agent thoughts
                        # go to a separate "-thought" artifact and aren't part of the
                        # main response stream the client renders).
                        if not metadata.get("intermediate_output") and item.content:
                            streamed_chars += len(item.content)

                    # Pass per-artifact first_chunk_sent flags and update after each chunk
                    first_chunk_sent, first_intermediate_chunk_sent = await self._handle_stream_item(
                        item,
                        updater,
                        task,
                        is_final=is_final,
                        streaming_artifact_id=streaming_artifact_id,
                        first_chunk_sent=first_chunk_sent,
                        first_intermediate_chunk_sent=first_intermediate_chunk_sent,
                        active_extensions=requested_extensions,
                        streamed_chars=streamed_chars,
                    )

                # Check for steering messages that arrived after the last abefore_model
                if (
                    deferred_terminal_item is not None
                    and deferred_terminal_item.state == TaskState.TASK_STATE_COMPLETED
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

                # Phantom-delegation guard: the model claimed completion with
                # include_subagent_output=true but never actually delegated (no
                # `task` ToolMessage this turn), which yields an empty response.
                # Re-enter once with a corrective nudge so it performs the
                # delegation instead of surfacing nothing.
                if (
                    deferred_terminal_item is not None
                    and deferred_terminal_item.state == TaskState.TASK_STATE_COMPLETED
                    and delegation_reinvocations < MAX_DELEGATION_REINVOCATIONS
                ):
                    # Reuse the state the agent already read at end-of-stream (carrier),
                    # instead of re-reading the checkpoint here (~1.5–5s).
                    phantom_values = turn_state.final_values or {}
                    if StreamHandler.is_phantom_subagent_completion(phantom_values):
                        delegation_reinvocations += 1
                        message_parts = [Part(text=_DELEGATION_NUDGE)]
                        resume_value = None  # fresh corrective turn, not a resume
                        logger.warning(
                            "[DELEGATION] include_subagent_output=true but no sub-agent ran this turn — "
                            "re-entering to force delegation (%d/%d)",
                            delegation_reinvocations,
                            MAX_DELEGATION_REINVOCATIONS,
                        )
                        continue  # Loop back for another stream round

                # Emit feedback request for complex tasks before terminal event
                if (
                    deferred_terminal_item is not None
                    and deferred_terminal_item.state in (TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED)
                    and requested_extensions is not None
                    and FEEDBACK_REQUEST_EXTENSION in requested_extensions
                ):
                    force_feedback = request_metadata.get("forceFeedbackRequest") in (True, "true", "1")
                    feedback_threshold = int(os.environ.get("FEEDBACK_RECURSION_THRESHOLD", "40"))
                    try:
                        # Reuse the agent's end-of-stream state (carrier) instead of re-reading.
                        msgs = (turn_state.final_values or {}).get("messages", [])
                        tool_msg_count = count_tool_messages(turn_state.final_values or {})
                        if force_feedback or tool_msg_count > feedback_threshold:
                            # Extract sub-agent IDs from activity-log metadata
                            # Prefer integer sub_agent_id; fall back to agent_name for built-in agents
                            sub_agents_set: set[str] = set()
                            for m in msgs:
                                meta = getattr(m, "additional_kwargs", {}).get("a2a_metadata", {})
                                if meta.get("sub_agent_id") is not None:
                                    sub_agents_set.add(str(meta["sub_agent_id"]))
                                elif meta.get("agent_name"):
                                    sub_agents_set.add(meta["agent_name"])
                            sub_agents = list(sub_agents_set)
                            await updater.update_status(
                                TaskState.TASK_STATE_WORKING,
                                new_feedback_request_message(
                                    context_id=task.context_id,
                                    task_id=task.id,
                                    sub_agents_involved=sub_agents,
                                ),
                            )
                            logger.info(
                                f"[FEEDBACK] Emitted feedback request (tool_msgs={tool_msg_count}, "
                                f"threshold={feedback_threshold}, sub_agents={sub_agents})"
                            )
                    except Exception:
                        logger.debug("[FEEDBACK] Could not emit feedback request", exc_info=True)

                # Emit the deferred terminal event
                if deferred_terminal_item is not None:
                    metadata = deferred_terminal_item.metadata or {}
                    if metadata.get("streaming_chunk"):
                        is_final = True
                    else:
                        # Reuse the agent's end-of-stream interrupts (carrier) instead of
                        # re-reading. This preserves the "contradictory completed non-final"
                        # guard (1099): a COMPLETED item is treated as final unless the
                        # captured state shows pending interrupts.
                        is_final = not turn_state.has_interrupts
                    first_chunk_sent, first_intermediate_chunk_sent = await self._handle_stream_item(
                        deferred_terminal_item,
                        updater,
                        task,
                        is_final=is_final,
                        streaming_artifact_id=streaming_artifact_id,
                        first_chunk_sent=first_chunk_sent,
                        first_intermediate_chunk_sent=first_intermediate_chunk_sent,
                        active_extensions=requested_extensions,
                        streamed_chars=streamed_chars,
                    )
                break  # Done — no re-invocation needed
        except asyncio.CancelledError:
            logger.info(f"Orchestrator execution cancelled for context_id={context_id}")
            try:
                await asyncio.shield(
                    updater.update_status(
                        TaskState.TASK_STATE_CANCELED,
                        new_text_message("Agent execution was cancelled.", context_id=task.context_id, task_id=task.id),
                    )
                )
            except (asyncio.CancelledError, Exception):
                pass  # Best-effort: queue may already be closed
            raise
        except Exception as e:
            logger.error(f"An error occurred while streaming the response: {e.__class__.__name__}: {e}", exc_info=True)
            raise InternalError() from e
        finally:
            # Persist any bypass rules that were approved during this turn.
            # Best-effort: failures are logged but don't affect the response.
            if user_config is not None:
                pending_bypass = getattr(user_config, "_pending_bypass_rules", None)
                if pending_bypass:
                    try:
                        await self.registry_service.persist_bypass_rules(
                            access_token=user_token,
                            pending_rules=pending_bypass,
                        )
                    except Exception:
                        logger.warning("Failed to persist bypass rules", exc_info=True)

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
        first_intermediate_chunk_sent: bool = False,
        active_extensions: set[str] | None = None,
        streamed_chars: int = 0,
    ) -> tuple[bool, bool]:
        """Handle a stream item from the agent and update the task accordingly.

        Streaming chunks (metadata.streaming_chunk=True) are emitted as
        TaskArtifactUpdateEvents. The FIRST chunk for a given artifact_id must
        be a create (append=False) so the A2A SDK registers the artifact;
        subsequent chunks use append=True. Main content and intermediate
        (sub-agent thought) chunks use separate artifact IDs and are therefore
        tracked independently.
        Status updates and terminal events use TaskStatusUpdateEvents.

        Args:
            item: The stream response item
            updater: Task updater for sending events
            task: Current task
            is_final: Whether this is the final state
            streaming_artifact_id: Stable artifact ID for streaming chunks
            first_chunk_sent: Whether the first main-artifact chunk has been sent
            first_intermediate_chunk_sent: Whether the first intermediate-artifact chunk has been sent

        Returns:
            Updated (first_chunk_sent, first_intermediate_chunk_sent) tuple
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
                return first_chunk_sent, first_intermediate_chunk_sent  # Client didn't request this extension
            source = metadata.get("source")
            logger.info(f"[ACTIVITY_LOG] Emitting status update: source={source}, content: {content[:50]}")
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                new_activity_log_message(
                    content,
                    task.context_id,
                    task.id,
                    source=source,
                ),
            )
            return first_chunk_sent, first_intermediate_chunk_sent  # Don't modify flags

        # --- Work plan items (todo snapshots) → status-update with DataPart extension ---
        if metadata.get("work_plan"):
            if not _ext_active(WORK_PLAN_EXTENSION):
                return first_chunk_sent, first_intermediate_chunk_sent  # Client didn't request this extension
            todos = metadata.get("todos", [])
            logger.info(f"[WORK_PLAN] Emitting work plan with {len(todos)} todos")
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                new_work_plan_message(
                    todos,
                    task.context_id,
                    task.id,
                ),
            )
            return first_chunk_sent, first_intermediate_chunk_sent  # Don't modify flags

        # --- Streaming content chunks → artifact-append ---
        # NOTE: Using proper A2A artifact-append protocol
        # Known limitation: The A2A client library may buffer artifact-update events
        # until the next status-update event arrives (SSE buffering in the Python client)
        # Streaming chunks will appear with slight delay until next natural status update
        # (which happens frequently during LLM token streaming)
        if state == TaskState.TASK_STATE_WORKING and metadata.get("streaming_chunk"):
            # Intermediate-output chunks (sub-agent thoughts, orchestrator reasoning) go into
            # a SEPARATE artifact stream so they don't mix with the main response artifact.
            # Main response chunks use streaming_artifact_id; thoughts use a "-thought" suffix.
            # This also ensures first_chunk_sent correctly reflects MAIN CONTENT only, so that
            # include_subagent_output=True can still produce a non-streaming final response.
            is_intermediate = bool(metadata.get("intermediate_output"))
            if is_intermediate:
                effective_artifact_id = streaming_artifact_id + "-thought"
            else:
                effective_artifact_id = streaming_artifact_id

            # A2A protocol: first chunk for an artifact_id MUST be a create
            # (append=False) so the SDK registers the artifact; subsequent
            # chunks may append (append=True). Sending append=True for the
            # very first chunk causes the A2A layer to drop the bytes with:
            #   "Received append=True for nonexistent artifact index ... Ignoring chunk."
            # Track main and intermediate artifact creation independently because
            # they use distinct artifact IDs.
            if is_intermediate:
                append = first_intermediate_chunk_sent
            else:
                append = first_chunk_sent

            # Determine artifact extensions for intermediate output (sub-agent thoughts)
            artifact_extensions = [INTERMEDIATE_OUTPUT_EXTENSION] if is_intermediate else None
            # If intermediate output extension isn't active, suppress entirely (don't leak reasoning to clients)
            if artifact_extensions and not _ext_active(INTERMEDIATE_OUTPUT_EXTENSION):
                return first_chunk_sent, first_intermediate_chunk_sent

            logger.info(
                f"[STREAMING] Calling add_artifact: len={len(content)}, append={append}, artifact_id={effective_artifact_id}"
            )
            # Keep agent_name in artifact metadata for attribution
            artifact_metadata = {}
            if metadata.get("agent_name"):
                artifact_metadata["agent_name"] = metadata["agent_name"]
            await updater.add_artifact(
                [Part(text=content)],
                artifact_id=effective_artifact_id,
                append=append,
                last_chunk=False,
                metadata=artifact_metadata or {},
                extensions=artifact_extensions,
            )
            logger.info("[STREAMING] Artifact chunk enqueued")
            # Intermediate-output chunks are supplementary (sub-agent thinking).
            # They must NOT claim first_chunk_sent (main) — the main response comes later
            # either as regular orchestrator streaming tokens or via include_subagent_output.
            # If we set first_chunk_sent=True here the executor would skip emitting the
            # include_subagent_output content and the final answer would never reach the user.
            if is_intermediate:
                return first_chunk_sent, True  # Mark intermediate artifact created
            return True, first_intermediate_chunk_sent  # Mark main artifact created

        # Handle different A2A task states
        if state == TaskState.TASK_STATE_WORKING and not is_final:
            # Status update or intermediate progress
            logger.info(f"Emitting status update: {content}")
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                new_text_message(content, context_id=task.context_id, task_id=task.id),
                metadata=metadata or None,
            )

        elif state == TaskState.TASK_STATE_WORKING and is_final:
            # Working state with no pending interrupts - still working
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                new_text_message(content, context_id=task.context_id, task_id=task.id),
                metadata=metadata or None,
            )

        elif state == TaskState.TASK_STATE_FAILED:
            # Handle failure state (terminal state - stream will close)
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message(content, context_id=task.context_id, task_id=task.id),
            )

        elif state == TaskState.TASK_STATE_INPUT_REQUIRED:
            # User input required - leave task in input_required state
            action_requests = item.action_requests
            if action_requests and _ext_active(HUMAN_IN_THE_LOOP_EXTENSION):
                # Structured HITL interrupt via extension — any A2A client can respond
                # review_configs are provided by the ConditionalHumanInTheLoopMiddleware
                review_configs = item.review_configs or [
                    {"action_name": ar.get("name", ""), "allowed_decisions": ["approve", "reject"]}
                    for ar in action_requests
                ]
                msg = new_hitl_interrupt_message(
                    description=content,
                    action_requests=action_requests,
                    review_configs=review_configs,
                    context_id=task.context_id,
                    task_id=task.id,
                )
                # Close any open streaming artifact before emitting the structured
                # HITL status. A turn that streamed orchestrator tokens before
                # hitting a guarded tool would otherwise leave the artifact stream
                # permanently open on the client. NOTE: unlike the plain-text
                # branches below, we do NOT set final_answer_source here — HITL is
                # intentionally not routed through the artifact text-fallback
                # (clients need the structured message); we only seal the stream.
                if first_chunk_sent:
                    await updater.add_artifact(
                        [Part(text="")],
                        artifact_id=streaming_artifact_id,
                        append=True,
                        last_chunk=True,
                        metadata={},
                    )
                await updater.update_status(
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    msg,
                )
            else:
                # Generic input_required (no HITL extension or not subscribed).
                #
                # CONTRACT FOR CLIENTS: Mirror the `completed` path — the terminal
                # input_required status ALWAYS carries the authoritative
                # FinalResponseSchema.message in its message body, and (if the
                # orchestrator streamed token chunks this turn) we close the
                # streaming artifact cleanly first. This guarantees the user
                # receives the orchestrator's reply even if intermediate SSE
                # artifact frames were dropped or the client only renders status
                # messages. The `final_answer_source: "fallback"` metadata flag
                # signals well-behaved clients to dedupe against the artifact.
                final_answer = content if content else "Additional input is required to continue."
                msg = new_text_message(final_answer, context_id=task.context_id, task_id=task.id)
                if item.interrupt_reason:
                    msg.metadata = {"interrupt_reason": item.interrupt_reason}
                await self._close_streaming_artifact_and_respond(
                    updater,
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    msg,
                    streaming_artifact_id=streaming_artifact_id,
                    first_chunk_sent=first_chunk_sent,
                    streamed_chars=streamed_chars,
                    final_message_len=len(final_answer),
                    base_metadata=metadata,
                )

        elif state == TaskState.TASK_STATE_AUTH_REQUIRED:
            # Authentication required - leave task in auth_required state.
            #
            # CONTRACT FOR CLIENTS: Mirror the `completed` path — the terminal
            # auth_required status ALWAYS carries the authoritative
            # FinalResponseSchema.message in its message body, and (if the
            # orchestrator streamed token chunks this turn) we close the
            # streaming artifact cleanly first. The `final_answer_source:
            # "fallback"` metadata flag signals well-behaved clients to dedupe
            # against the artifact text they already rendered.
            final_answer = content if content else "Authentication is required to continue."
            await self._close_streaming_artifact_and_respond(
                updater,
                TaskState.TASK_STATE_AUTH_REQUIRED,
                new_text_message(final_answer, context_id=task.context_id, task_id=task.id),
                streaming_artifact_id=streaming_artifact_id,
                first_chunk_sent=first_chunk_sent,
                streamed_chars=streamed_chars,
                final_message_len=len(final_answer),
                base_metadata=metadata,
            )

        elif state == TaskState.TASK_STATE_COMPLETED and is_final:
            # Task completed successfully.
            #
            # CONTRACT FOR CLIENTS: The terminal `completed` status ALWAYS carries the
            # authoritative final answer in its message body (the validated
            # FinalResponseSchema.message). This is true for both the streamed and
            # non-streamed branches. Clients that already rendered the streamed
            # artifact chunks should treat this terminal message as the source of
            # truth (dedupe / replace) rather than appending it — the
            # `final_answer_source: "fallback"` metadata flag on the status update
            # signals that the same text was also delivered via artifact-append.
            # This guarantees the user receives the reply even if any intermediate
            # SSE artifact frame fails to parse on the client side.
            final_answer = content if content else "Task completed successfully"
            # Streamed and non-streamed completions converge here: the helper
            # closes the streaming artifact (only when token chunks were streamed
            # this turn) and emits the terminal `completed` status carrying the
            # authoritative final answer, tagged `final_answer_source: "fallback"`
            # when it duplicates already-streamed artifact text.
            await self._close_streaming_artifact_and_respond(
                updater,
                TaskState.TASK_STATE_COMPLETED,
                new_text_message(final_answer, context_id=task.context_id, task_id=task.id),
                streaming_artifact_id=streaming_artifact_id,
                first_chunk_sent=first_chunk_sent,
                streamed_chars=streamed_chars,
                final_message_len=len(final_answer),
                base_metadata=metadata,
            )

        elif state == TaskState.TASK_STATE_COMPLETED and not is_final:
            logger.info(f"Contradictory completed non-final state, treating as input_required: {content}")
            # User input required - leave task in input_required state
            await updater.update_status(
                TaskState.TASK_STATE_INPUT_REQUIRED,
                new_text_message(content, context_id=task.context_id, task_id=task.id),
            )
        else:
            # Unknown state - log warning and treat as completed
            logger.warning(f"Unknown task state: {state}, treating as completed")
            await updater.add_artifact(
                [Part(text=content)],
                name="orchestrator_result",
            )
            await updater.complete()

        # Return flags unchanged for non-streaming paths
        return first_chunk_sent, first_intermediate_chunk_sent

    async def _close_streaming_artifact_and_respond(
        self,
        updater: TaskUpdater,
        state: TaskState,
        msg: Message,
        *,
        streaming_artifact_id: str,
        first_chunk_sent: bool,
        streamed_chars: int,
        final_message_len: int,
        base_metadata: dict | None,
    ) -> None:
        """Seal an open streaming artifact (if any) and emit a terminal status update.

        Shared by the ``input_required``, ``auth_required`` and ``completed``
        branches. The artifact (if one was streamed) is closed with a final empty
        chunk, then the terminal status is emitted.

        SINGLE-SOURCE EMISSION: when the full answer was streamed as artifact
        chunks, the streamed artifact *is* the answer — re-sending it in the
        terminal ``status.message`` would duplicate it for every consumer (web
        render + persistence, slack, google-chat). So in that case emit a BARE
        completion (state only) and let clients use the streamed artifact.

        The terminal message stays authoritative only when the answer was NOT
        fully streamed:
        - interrupts (``input_required`` / ``auth_required`` carry the message);
        - answers assembled at the terminal (e.g. ``include_subagent_output``,
          where nothing — or only a partial prefix — was streamed to the main
          artifact). A streamed partial prefix is tagged ``final_answer_source``
          so consumers can still dedupe it.
        """
        answer_fully_streamed = (
            state == TaskState.TASK_STATE_COMPLETED
            and first_chunk_sent
            and final_message_len > 0
            and streamed_chars >= final_message_len
        )
        if first_chunk_sent:
            await updater.add_artifact(
                [Part(text="")],
                artifact_id=streaming_artifact_id,
                append=True,
                last_chunk=True,
                metadata={},
            )
            logger.info(
                "[STREAMING] Completion: artifact_id=%s streamed_chars=%d "
                "final_message_len=%d task_state=%s fully_streamed=%s",
                streaming_artifact_id,
                streamed_chars,
                final_message_len,
                state,
                answer_fully_streamed,
            )
        if answer_fully_streamed:
            # Answer already delivered via the streamed artifact — emit a bare
            # completion (no message) so it isn't re-sent / re-persisted / re-rendered.
            await updater.update_status(state, None, metadata=(base_metadata or None))
        else:
            status_metadata = dict(base_metadata) if base_metadata else {}
            if first_chunk_sent:
                # Only a partial prefix was streamed (rare — e.g. a non-empty message
                # alongside include_subagent_output). Tag so consumers can dedupe it.
                status_metadata["final_answer_source"] = "fallback"
            await updater.update_status(state, msg, metadata=status_metadata or None)

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
                    state=TaskState.TASK_STATE_CANCELED,
                    message=new_text_message("Agent execution was cancelled.", context_id=context_id, task_id=task_id),
                ),
            )
        )
