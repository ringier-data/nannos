"""
Graph factory for OrchestratorDeepAgent.

This module provides a centralized factory for creating and managing LangGraph instances.
It encapsulates all graph-related concerns:
- Model creation and caching (Bedrock vs OpenAI)
- Checkpointer setup (PostgreSQL)
- Middleware stack assembly
- Graph creation and caching per model type

Architecture:
- ONE universal graph per model type (Bedrock vs OpenAI)
- All graphs share a single checkpointer for conversation continuity
- Tools are injected at runtime via GraphRuntimeContext (not baked into graphs)
- DynamicToolDispatchMiddleware handles dynamic tool binding and dispatch
"""

import logging
import os
import uuid as _uuid
from typing import Any, Optional

from a2a.types import Message as A2AMessage
from a2a.types import Role as A2ARole
from agent_common.a2a.client_runnable import A2AClientRunnable as _ClientRunnable
from agent_common.a2a.structured_response import A2A_PROTOCOL_ADDENDUM as SUB_AGENT_PROTOCOL_ADDENDUM
from agent_common.a2a.structured_response import get_response_format as get_sub_agent_response_format
from agent_common.a2a.structured_response import select_response_format
from agent_common.core.copy_file_tool import create_copy_file_tool
from agent_common.core.graph_utils import (
    build_code_interpreter_middlewares,
    build_common_middleware_stack,
    create_indexing_backend_factory,
)
from agent_common.core.model_factory import (
    _has_aws_credentials,
    create_model,
    is_gemini_model,
    require_default_model,
)
from agent_common.core.tool_risk_scorer import score_tool_risk
from agent_common.middleware.conditional_hitl import ConditionalHumanInTheLoopMiddleware
from agent_common.middleware.conversation_context_tools_middleware import ConversationContextToolsMiddleware
from agent_common.middleware.prompt_caching import LiteLLMPromptCachingMiddleware
from agent_common.middleware.steering_middleware import SteeringMiddleware
from agent_common.middleware.storage_paths_middleware import StoragePathsInstructionMiddleware
from agent_common.middleware.tool_status import ToolStatusMiddleware
from agent_common.models.base import ModelType, ThinkingLevel
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from ringier_a2a_sdk.cost_tracking import CostLogger

from ..handlers import handle_tool_failure, should_retry
from ..middleware import (
    A2ATaskTrackingMiddleware,
    AuthErrorDetectionMiddleware,
    DynamicToolDispatchMiddleware,
    RepeatedToolCallMiddleware,
    TodoStatusMiddleware,
    UserPreferencesMiddleware,
)
from ..middleware.error_classification_middleware import ErrorClassificationMiddleware
from ..middleware.final_response_strip_middleware import FinalResponseTextStripMiddleware
from ..middleware.playbook_middleware import PlaybookInjectionMiddleware
from ..models.config import AgentSettings, GraphRuntimeContext
from ..models.schemas import FinalResponseSchema
from .file_tools import create_presigned_url_tool
from .steering_state import get_all_active_subagent_dispatches, get_orchestrator_pending_messages
from .time_tools import create_time_tool

logger = logging.getLogger(__name__)


def _create_hitl_middleware() -> ConditionalHumanInTheLoopMiddleware:
    """Create a ConditionalHumanInTheLoopMiddleware instance for dynamic risk scoring.

    All tool guarding is now handled by the dynamic risk scoring system:
    - Static guards (self-improvement, privacy, bug reports) are stored in the
      tool_risk_scores DB table with base_score=1.0 (always interrupt).
    - Other tools are scored by LLM at runtime and interrupt if score >= threshold.
    """
    return ConditionalHumanInTheLoopMiddleware(
        interrupt_on=None,
        risk_scorer=score_tool_risk,
        default_risk_threshold=0.8,
    )


class GraphFactory:
    """Factory for creating and managing LangGraph instances.

    Centralizes all graph-related concerns:
    - Model creation/caching (Bedrock vs OpenAI)
    - Checkpointer (shared PostgreSQL instance)
    - Middleware stack
    - Graph creation/caching per model type

    Architecture:
    - ONE graph per model type, shared across all users
    - Single checkpointer for conversation continuity across model switches
    - Tools injected at runtime via GraphRuntimeContext
    - DynamicToolDispatchMiddleware handles tool binding and dispatch

    Usage:
        factory = GraphFactory(config, thinking_level=None)
        graph = factory.get_graph("claude-sonnet-4.5")
        # Use graph.astream(..., context=runtime_context)
    """

    def __init__(
        self,
        config: AgentSettings,
        a2a_middleware: Optional[A2ATaskTrackingMiddleware] = None,
        cost_logger: Optional[CostLogger] = None,
    ):
        """Initialize the graph factory.

        Args:
            config: Agent settings with model config, checkpoint config, etc.
            a2a_middleware: Optional A2A task tracking middleware (shared with discovery)
            cost_logger: Optional CostLogger instance for cost tracking callbacks
        """
        self.config = config
        self.cost_logger = cost_logger

        # Model and graph caches
        self._models: dict[tuple[str, str | None], BaseChatModel] = {}
        self._graphs: dict[tuple[str, str | None], CompiledStateGraph] = {}
        self._task_scheduler_graphs: dict[tuple[str, str | None], CompiledStateGraph] = {}

        # Static tools cache (created once per model type, reused)
        self._static_tools_cache: list[BaseTool] = []

        # Create shared checkpointer for all graphs
        # Pool is opened in ensure_store_setup(); _checkpointer_pool is None when MemorySaver is used.
        self._checkpointer_pool = None
        self._checkpointer: BaseCheckpointSaver = self._create_checkpointer(config)

        # Shared document store (PostgreSQL + pgvector), optional. When PostgreSQL isn't
        # configured the store is None and the agent falls back to ephemeral StateBackend.
        #
        # Embeddings are resolved LAZILY (see _resolve_store_mode / ensure_store_ready), NOT
        # here: at construction the gateway/console caches can be cold (pod boots before they
        # are reachable), and resolving eagerly would latch the store off for the whole
        # process lifetime on a transient cold-start failure. The store self-heals instead —
        # it retries a cold gateway and upgrades to a semantic index once an embedding default
        # is configured, without a restart.
        self._postgres_conn: str | None = None
        self._embeddings_model = None
        self._store_enabled = bool(config.POSTGRES_HOST and config.POSTGRES_PASSWORD)
        # Store readiness state: None = undecided/transient (retry, don't cache), "absent" =
        # no embedding default configured (stable, store-less, watched for one), "indexed" =
        # embeddings resolved (terminal). Drives store building and graph caching.
        self._store_mode: str | None = None

        if self._store_enabled:
            self._postgres_conn = (
                f"postgresql://{config.POSTGRES_USER}:{config.POSTGRES_PASSWORD}"
                f"@{config.POSTGRES_HOST}:{config.POSTGRES_PORT}/{config.POSTGRES_DB}"
            )
        else:
            logger.info(
                "PostgreSQL not configured – document store disabled. "
                "Set POSTGRES_HOST and POSTGRES_PASSWORD to enable persistent document storage."
            )

        self._store = None
        self._connection_pool = None
        self._store_setup_complete: bool = False

        # Create middleware instances (shared across all graphs)
        self._a2a_middleware = a2a_middleware or A2ATaskTrackingMiddleware()
        self._auth_middleware = AuthErrorDetectionMiddleware()
        self._todo_middleware = TodoStatusMiddleware()
        self._loop_detection_middleware = RepeatedToolCallMiddleware(max_repeats=5, max_tool_repeats=10, window_size=10)
        self._retry_middleware = ToolRetryMiddleware(
            max_retries=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_FACTOR,
            retry_on=should_retry,
            on_failure=handle_tool_failure,
        )
        logger.debug("Initialized middleware stack")

    def _create_checkpointer(self, config: AgentSettings) -> BaseCheckpointSaver:
        """Create the connection pool and return a MemorySaver placeholder.

        AsyncPostgresSaver.__init__ calls asyncio.get_running_loop() so it must be
        constructed inside an async context.  This method creates the pool (safe to
        do synchronously) and stores it on self._checkpointer_pool.
        _setup_checkpointer() — called from ensure_store_setup() — instantiates
        AsyncPostgresSaver and replaces self._checkpointer before any requests are served.
        """
        from langgraph.checkpoint.memory import MemorySaver
        from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import (
            build_checkpointer_pool,
            memory_fallback_allowed,
            missing_host_error,
        )

        if not config.POSTGRES_HOST:
            if not memory_fallback_allowed():
                raise missing_host_error()
            logger.warning(
                "POSTGRES_HOST not set – using in-memory checkpointer (conversations will not persist across restarts)"
            )
            return MemorySaver()

        # The checkpointer reuses the service's main POSTGRES_* connection (same DB/user
        # as the document store); POSTGRES_SCHEMA places the tables in the service's own
        # schema via search_path, consistent with the per-service-schema convention.
        pool = build_checkpointer_pool(
            host=config.POSTGRES_HOST,
            port=str(config.POSTGRES_PORT),
            db=config.POSTGRES_DB,
            user=config.POSTGRES_USER,
            password=config.POSTGRES_PASSWORD,
            schema=config.POSTGRES_SCHEMA,
        )
        self._checkpointer_pool = pool

        logger.info(
            "Prepared PostgreSQL checkpointer pool (host=%s, db=%s, schema=%s) — "
            "AsyncPostgresSaver will be created in _setup_checkpointer()",
            config.POSTGRES_HOST,
            config.POSTGRES_DB,
            config.POSTGRES_SCHEMA or "<role default>",
        )
        # Placeholder replaced by AsyncPostgresSaver in _setup_checkpointer()
        return MemorySaver()

    @property
    def checkpointer(self) -> BaseCheckpointSaver:
        """Get the shared checkpointer instance."""
        return self._checkpointer

    def _resolve_store_mode(self) -> None:
        """Lazily resolve the embedding model that backs the document store's semantic index.

        Sets self._store_mode to one of:
          - "indexed": a default embedding model resolved → semantic index available (terminal).
          - "absent":  the defaults endpoint answered and no embedding default is set — a
                       stable, supported state (indexing is optional). Cacheable, but
                       ensure_store_ready() upgrades it to "indexed" if an admin sets one.
          - None:      transient/cold (gateway or console not reachable yet) — the caller must
                       NOT build/cache a store and should retry on the next readiness check.

        This is the single point that turns a cold-start hiccup into a retry instead of a
        process-lifetime latch (the bug this whole machinery fixes)."""
        if self._store_mode == "indexed":
            return
        from agent_common.core.model_factory import (
            EmbeddingModelNotConfigured,
            create_embeddings,
            embedding_default_known_absent,
        )

        try:
            self._embeddings_model = create_embeddings()
            self._store_mode = "indexed"
            logger.info("Document store: gateway embeddings resolved; semantic index enabled")
        except EmbeddingModelNotConfigured:
            if embedding_default_known_absent():
                if self._store_mode != "absent":
                    logger.info(
                        "Document store: no default embedding model configured; semantic index "
                        "disabled until one is set in the console"
                    )
                self._store_mode = "absent"
            else:
                self._store_mode = None  # transient/cold — retry later
        except Exception as e:
            self._store_mode = None  # transient (gateway unreachable, etc.) — retry later
            logger.debug("Document store: embeddings not resolvable yet (%s); will retry", e)

    def _reset_store(self) -> None:
        """Drop the cached store and graphs so the next readiness check rebuilds with a
        semantic index. Used when an embedding default appears after we settled store-less.
        The connection pool is index-agnostic and is reused."""
        self._store = None
        self._store_mode = None
        self._store_setup_complete = False
        self._embeddings_model = None
        self._graphs.clear()
        self._task_scheduler_graphs.clear()

    @property
    def store(self):
        """Get the shared document store instance (or None).

        Returns None when PostgreSQL is not configured, AND when embeddings are still resolving
        on a cold start (transient): in that case nothing is built or cached, so a later access
        (driven by ensure_store_ready) retries. Builds + caches the store once the mode is
        decided ("indexed" → with semantic index, "absent" → store-less).

        IMPORTANT: call ensure_store_ready() before relying on the store — it opens the pool
        and runs schema setup, and is what drives the transient retry / self-heal.
        """
        if not self._store_enabled:
            return None
        if self._store is not None:
            return self._store

        if self._store_mode not in ("indexed", "absent"):
            self._resolve_store_mode()
        if self._store_mode is None:
            return None  # transient — don't build/cache; ensure_store_ready() retries

        from langgraph.store.postgres.aio import AsyncPostgresStore
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        # Create connection pool for AsyncPostgresStore (create once and reuse)
        # Pool will be opened explicitly in ensure_store_ready()
        if self._connection_pool is None:
            self._connection_pool = AsyncConnectionPool(
                self._postgres_conn,
                min_size=2,
                max_size=10,
                open=False,  # Don't open in constructor (deprecated)
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )

        index_config = None
        if self._embeddings_model is not None:
            from agent_common.core.model_factory import get_embedding_dimension

            index_config = {
                # Single source of truth: same dimension create_embeddings() requests,
                # so the index and the produced vectors always agree.
                "dims": get_embedding_dimension(),
                "embed": self._embeddings_model,
                "fields": ["contextualized_content"],
            }

        self._store = AsyncPostgresStore(
            conn=self._connection_pool,
            index=index_config,
        )
        if index_config:
            logger.info(
                "Initialized AsyncPostgresStore with gateway embeddings (%d dims) and connection pool",
                index_config["dims"],
            )
        else:
            logger.info("Initialized AsyncPostgresStore without semantic indexing (no embeddings)")
        return self._store

    @property
    def backend_factory(self) -> Any:
        """Get the backend for FilesystemMiddleware.

        Returns a ``CompositeBackend`` with:
        - Default: ``StateBackend`` (ephemeral storage in agent state)
        - ``/memories/``: ``IndexingStoreBackend`` (persistent storage with semantic indexing)

        Delegates to ``create_indexing_backend_factory`` from ``agent_common``.

        Returns:
            A ``BackendProtocol`` instance (``CompositeBackend`` or ``StateBackend``)
        """
        backend = create_indexing_backend_factory(
            self.store,
            cost_logger=self.cost_logger,
            include_attachments=True,
        )

        return backend

    async def ensure_store_setup(self) -> None:
        """Startup hook: set up the checkpointer once, then attempt document-store setup.

        The store attempt is best-effort here — if the gateway/console is cold at boot it stays
        unready and ensure_store_ready() (called per request) retries it. Safe to call multiple
        times; the checkpointer setup is idempotent.
        """
        await self._setup_checkpointer()
        await self.ensure_store_ready()

    async def ensure_store_ready(self) -> None:
        """Per-request, idempotent document-store readiness check. Cheap once the store is set
        up (an early return).

        This is what makes the store self-heal without a restart:
          - a transient cold-start (gateway/console unreachable at boot) leaves the mode
            undecided, so this is a no-op now and retried on the next request;
          - if we settled store-less because no embedding default was configured, but an admin
            has since set one, the cached store-less store + graphs are dropped and rebuilt
            with a semantic index.

        Graphs built while this hasn't reached a decided mode are intentionally not cached
        (see get_graph), so they rebuild once the store resolves.
        """
        if not self._store_enabled:
            return

        from agent_common.core.model_factory import is_embeddings_configured

        # Self-heal: an embedding default appeared after we settled store-less → rebuild indexed.
        if self._store_mode == "absent" and is_embeddings_configured():
            logger.info("Document store: embedding default now configured; rebuilding with semantic index")
            self._reset_store()

        if self._store_setup_complete:
            return

        self._resolve_store_mode()
        if self._store_mode is None:
            return  # transient/cold — retry on the next request

        store = self.store  # builds the store object (indexed iff embeddings resolved)
        if store is None:
            return  # defensive: still not ready

        # Open connection pool if not already open
        if self._connection_pool is not None and not self._connection_pool._opened:
            await self._connection_pool.open()
            logger.info("Opened AsyncConnectionPool for document store")

        # Set up the schema (idempotent; safe to call repeatedly)
        await store.setup()
        self._store_setup_complete = True
        logger.info("Document store ready (mode=%s)", self._store_mode)

    async def _setup_checkpointer(self) -> None:
        """Instantiate AsyncPostgresSaver, open pool, verify PG ≥ 11, run migrations.

        Replaces the MemorySaver placeholder in self._checkpointer with the real saver.
        """
        pool = self._checkpointer_pool
        if pool is None:
            return  # permanent MemorySaver — nothing to do

        from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import (
            _verify_postgres_version,
            open_pool_if_closed,
        )

        await open_pool_if_closed(pool)
        logger.info("Checkpoint connection pool open")

        await _verify_postgres_version(pool)

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        serde = None
        if self.config.CHECKPOINT_S3_BUCKET_NAME:
            from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import S3OffloadingSerde

            threshold = int(self.config.CHECKPOINT_S3_THRESHOLD_MB * 1024 * 1024)
            serde = S3OffloadingSerde(bucket=self.config.CHECKPOINT_S3_BUCKET_NAME, threshold_bytes=threshold)
            logger.info("S3 checkpoint offloading enabled: %s", self.config.CHECKPOINT_S3_BUCKET_NAME)

        checkpointer = AsyncPostgresSaver(pool, serde=serde)
        await checkpointer.setup()
        self._checkpointer = checkpointer
        logger.info(
            "PostgreSQL checkpointer ready (tables in schema=%s)",
            self.config.POSTGRES_SCHEMA or "<role default>",
        )

    @property
    def a2a_middleware(self) -> A2ATaskTrackingMiddleware:
        """Get the A2A task tracking middleware (needed by discovery service)."""
        return self._a2a_middleware

    async def close(self) -> None:
        """Close connection pools and clean up resources on application shutdown."""
        if self.cost_logger is not None:
            await self.cost_logger.shutdown()
            logger.info("Cost logger shutdown complete")

        if self._checkpointer_pool is not None:
            from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import close_pool_if_open

            await close_pool_if_open(self._checkpointer_pool)
            logger.info("Closed AsyncConnectionPool for checkpointer")

        if self._connection_pool is not None and self._connection_pool._opened:
            await self._connection_pool.close()
            logger.info("Closed AsyncConnectionPool for document store")

    def _create_model(self, model_type: ModelType, thinking_level: Optional[ThinkingLevel]) -> BaseChatModel:
        """Create a model instance for the given model type.

        Args:
            model_type: The type of model to create ('gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4.5', 'claude-sonnet-4.6' or 'claude-haiku-4-5')

        Returns:
            BaseChatModel: The created model instance
        """
        # LLM cost is captured at the Model Gateway now (proxy CustomLogger);
        # the in-app CostTrackingCallback is intentionally NOT attached here to avoid
        # double-counting. (cost_logger remains for the embeddings path until Phase 5.)
        return create_model(model_type, thinking_level, callbacks=None)

    def _get_or_create_model(self, model_type: ModelType, thinking_level: Optional[ThinkingLevel]) -> BaseChatModel:
        """Get or create a model instance

        It will be cached by model_type AND thinking_level, even though ideally we could
        use with_config on the same model instance, but those parameters are not configurable that way yet.

        Args:
            model_type: The type of model ('gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4.5', 'claude-sonnet-4.6' or 'claude-haiku-4-5')

        Returns:
            BaseChatModel: The model instance (cached or newly created)
        """
        # Create compound cache key: (model_type, thinking_level)
        cache_key = (model_type, thinking_level)
        if cache_key not in self._models:
            logger.info(f"Creating model instance for: {model_type} with thinking_level={thinking_level}")
            self._models[cache_key] = self._create_model(model_type, thinking_level)
        return self._models[cache_key]

    def _create_middleware_stack(self, model: BaseChatModel | None = None) -> list[Any]:
        """Create the complete middleware stack for a graph.

        Middleware Execution Order (LangChain convention):
          - before_* hooks: First to last (list order)
          - after_*  hooks: Last to first (reverse order)
          - wrap_*   hooks: Nested — first middleware in the list wraps all
            others (outermost).  For ``awrap_tool_call`` the call path is:
                [0] → [1] → … → [N] → ToolNode handler
            So [0] can short-circuit before any later middleware sees the call.

        IMPORTANT: ``DynamicToolDispatchMiddleware`` is the first *tool-call*
        handler.  ``ConversationContextToolsMiddleware`` is placed before it for
        ``wrap_model_call`` (so its injected gated tool flows through
        DynamicToolDispatch's schema-cleanup pipeline) but it has no tool-call
        hook, so DynamicToolDispatch is still the effective ([0]) interceptor for
        the ``task`` tool A2A sub-agent dispatch.  Being the outermost tool-call
        handler means inner middlewares (Auth, Retry, …) never see the ``task``
        tool call — they only see the ToolMessage result if it was returned.
        This is why ``AuthErrorDetectionMiddleware`` cannot intercept A2A 401
        errors via ``interrupt()``.

        Returns:
            Ordered middleware list (first = outermost for wrap hooks).
        """
        # DynamicToolDispatchMiddleware must be first (outermost) to intercept
        # tool calls and short-circuit sub-agent dispatch before inner middlewares.
        # Add Static tools directly to the graph (not via middleware) in case you want
        # FinalResponseSchema's return_direct=True to be respected by the model
        dynamic_tool_middleware = DynamicToolDispatchMiddleware(
            static_tools=[],
            agent_settings=self.config,
            cost_logger=self.cost_logger,
        )

        # UserPreferencesMiddleware injects user preferences (language, etc.) into system prompt
        user_preferences_middleware = UserPreferencesMiddleware()

        # PlaybookInjectionMiddleware injects AGENTS.md and skill index into system prompt
        playbook_middleware = PlaybookInjectionMiddleware(store=self.store)

        # StoragePathsInstructionMiddleware adds filesystem storage paths documentation
        storage_paths_middleware = StoragePathsInstructionMiddleware()

        # ConversationContextToolsMiddleware gates conversation-context-specific tools
        # (e.g. read_personal_file) so they are bound only in the contexts where they
        # apply. The gated tool is user-scoped and lives in the runtime tool_registry
        # (not the static whitelist), so it is resolved by name at model-call time —
        # the orchestrator's single-graph-per-model cannot hold a per-user instance.
        # Placed outermost so the injected tool flows through DynamicToolDispatch's
        # schema-cleanup/dispatch pipeline; the gate has no tool-call hook, so
        # DynamicToolDispatch remains the effective first handler for tool dispatch.
        context_gate_middleware = ConversationContextToolsMiddleware(
            runtime_gated_tools={"read_personal_file": frozenset({"channel"})},
        )

        # Outermost → innermost (for wrap_* hooks):
        # DynamicToolDispatch[0] → StoragePaths → PromptCaching → Steering
        # → UserPreferences → LoopDetection → Auth → Retry → A2A → Todo[9]
        #
        # LiteLLMPromptCaching places the cache breakpoint after all static content (system prompt
        # + storage paths), so the cache is shared across all users. StoragePaths is included in the
        # cache. Steering comes after caching so follow-up messages aren't cached.
        # LoopDetection comes before Auth/Retry to catch loops early.

        async def _forward_to_active_subagents(context_id: str, messages: list) -> None:
            """Forward steering messages to all active sub-agents.

            When the orchestrator's SteeringMiddleware picks up follow-up
            messages, they are forwarded to every in-progress sub-agent so
            each sub-agent's own SteeringMiddleware can inject them.
            Multiple sub-agents may run in parallel (LangGraph ToolNode uses
            asyncio.gather for concurrent tool calls).
            """
            dispatches = get_all_active_subagent_dispatches(context_id)
            if not dispatches:
                return

            for dispatch in dispatches:
                runnable = dispatch.runnable
                if not isinstance(runnable, _ClientRunnable):
                    logger.debug(
                        f"[STEERING] Active sub-agent '{dispatch.subagent_name}' is local, "
                        "skipping A2A forwarding (local agents share the same steering queue)"
                    )
                    continue

                if not dispatch.subagent_context_id:
                    logger.warning(
                        f"[STEERING] Active sub-agent '{dispatch.subagent_name}' has no context_id yet, "
                        "cannot forward steering message"
                    )
                    continue

                for msg in messages:
                    if not msg.parts:
                        continue

                    # Forward all parts (text, files, data) — don't strip non-text content
                    steering_msg = A2AMessage(
                        role=A2ARole.ROLE_USER,
                        parts=msg.parts,
                        message_id=str(_uuid.uuid4()),
                        context_id=dispatch.subagent_context_id,
                        task_id=dispatch.subagent_task_id,
                    )
                    await runnable.send_steering_message(steering_msg)
                    logger.info(
                        f"[STEERING] Forwarded steering message to sub-agent "
                        f"'{dispatch.subagent_name}' (context_id={dispatch.subagent_context_id})"
                    )

        steering_middleware = SteeringMiddleware(
            get_pending_messages=get_orchestrator_pending_messages,
            on_messages_received=_forward_to_active_subagents,
        )

        # ConditionalHumanInTheLoopMiddleware: uses interrupt() to pause and ask for user
        # confirmation before executing guarded tools (self-improvement, privacy, bug reports).
        # Supports argument-based conditions (e.g., docstore_search only when include_personal=True).
        hitl_middleware = _create_hitl_middleware()

        # CodeInterpreterMiddleware exposes an ``eval`` JS REPL (with skills_backend).
        # The orchestrator passes ``broaden_exposure=False`` so ``eval`` exposes
        # only the filesystem baseline — NOT the per-user tool registry (hundreds
        # of MCP tools). The orchestrator's job is to plan and delegate via
        # ``task``; pulling the whole registry into the PTC prompt bloats it and
        # strips every dispatchable tool from the model's bound list, derailing it
        # into emitting a final response instead of dispatching. ``task``, ``eval``
        # and the response-schema tools remain visible to the model.
        code_interpreter_middlewares = build_code_interpreter_middlewares(
            self.backend_factory,
            broaden_exposure=False,
            risk_scorer=score_tool_risk,
            default_risk_threshold=0.8,
        )

        middleware_stack: list[Any] = [
            context_gate_middleware,
            dynamic_tool_middleware,
            storage_paths_middleware,
            # Place the cache breakpoint right after the static system prefix
            # (system prompt + storage paths) and before the per-user content
            # appended below (steering, user preferences, playbook). The marker is
            # injected as an OpenAI-format cache_control content block, which LiteLLM
            # translates to the provider's native format (Bedrock cachePoint /
            # Anthropic ephemeral) and ignores for non-caching providers — so no
            # provider gate is needed under the gateway architecture,
            # where the client is always ChatOpenAI rather than ChatBedrockConverse.
            LiteLLMPromptCachingMiddleware(),
            steering_middleware,
            user_preferences_middleware,
            playbook_middleware,
            *code_interpreter_middlewares,
            ToolStatusMiddleware(),
            self._loop_detection_middleware,
            self._auth_middleware,
            ErrorClassificationMiddleware(),
            hitl_middleware,
            self._retry_middleware,
            self._a2a_middleware,
            self._todo_middleware,
            # Innermost: strip duplicate plain-text content from AIMessages that
            # carry a FinalResponseSchema tool call, so every outer middleware and
            # the checkpointer see the cleaned message (prevents the model from
            # imitating its own text+tool-call pattern on later turns).
            FinalResponseTextStripMiddleware(),
        ]
        return middleware_stack

    def get_static_tools(self, with_response_tool: bool = False) -> list[BaseTool]:
        """Get static tools for the given model type.

        Returns:
            List of static tools (cached). When with_response_tool=True, returns a
            new list with FinalResponseSchema appended (does not pollute the cache).
        """
        if not self._static_tools_cache:
            static_tools: list[BaseTool] = []

            # Add time tool for current time and relative date calculations
            static_tools.append(create_time_tool())

            # Add presigned URL tool for dispatching files to sub-agents (requires AWS)
            if _has_aws_credentials():
                static_tools.append(create_presigned_url_tool())

            # Add copy_file tool for efficient file copying without LLM context loading
            static_tools.append(create_copy_file_tool(self.backend_factory))

            self._static_tools_cache = static_tools

        # Return a copy with FinalResponseSchema appended if needed,
        # to avoid polluting the shared cache for other models
        if with_response_tool:
            return list(self._static_tools_cache) + [
                StructuredTool.from_function(
                    func=lambda **kwargs: FinalResponseSchema(**kwargs),
                    name="FinalResponseSchema",
                    description=(
                        "ALWAYS use this tool to format your final response to the user. "
                        "Put the ENTIRE answer in the 'message' field and emit NO plain-text "
                        "content alongside this call — any text outside the tool call is "
                        "discarded and wastes tokens."
                    ),
                    args_schema=FinalResponseSchema,
                    return_direct=True,
                )
            ]

        return self._static_tools_cache

    def _create_graph(self, model_type: ModelType, thinking_level: Optional[ThinkingLevel]) -> CompiledStateGraph:
        """Create a graph for the given model type.

        Args:
            model_type: The type of model

        Returns:
            CompiledStateGraph: The newly created graph
        """
        model = self._get_or_create_model(model_type, thinking_level)

        # Ensure store is initialized before creating graph (required for longterm memory)
        # Access the store property to trigger lazy initialization (may be None if not configured)
        store_instance = self.store

        # Note: Sub-agents (both local and remote A2A) are now registered dynamically
        # via GraphRuntimeContext.subagent_registry at request time, not at graph creation.
        # This enables per-user sub-agent discovery and unified handling.
        #
        # The general-purpose agent is a DynamicLocalAgentRunnable registered in
        # subagent_registry as "general-purpose". It uses inject_all_tools to receive
        # ALL MCP tools from the orchestrator's tool_registry, and has
        # ToolsetSelectorMiddleware as extra_middlewares for smart tool filtering.
        #
        # The default GP from create_deep_agent is still created internally but never
        # invoked because DynamicToolDispatchMiddleware dispatches "general-purpose"
        # from subagent_registry (the DynamicLocalAgentRunnable) before it can fall through.

        backend = self.backend_factory

        # Gemini models get Google's server-side built-in tools (google_search,
        # code_execution), which can't coexist with a forced tool_choice — so they drive
        # both the built-in-tools binding below and the structured-output strategy. Detected
        # via is_gemini_model (NOT a bare "vertex" provider match — Vertex also hosts
        # Claude/Llama, which must not be bound Google's built-in tools).
        is_gemini = is_gemini_model(model_type)

        # Single source of truth for the provider -> structured-output strategy decision,
        # shared with the sub-agent path (agent_common.a2a.structured_response). Picks
        # ToolStrategy unless forcing tool_choice is unsafe (Anthropic+thinking, or
        # built-in tools present), in which case FinalResponseSchema is bound as a tool.
        response_format, requires_response_tool = select_response_format(
            model_type,
            FinalResponseSchema,
            thinking_enabled=bool(thinking_level),
            has_builtin_tools=is_gemini,
        )
        middleware = self._create_middleware_stack(model=model)
        static_tools_list = self.get_static_tools(with_response_tool=requires_response_tool)

        # Add Google built-in tools for Gemini models
        # These are passed via the tools parameter so create_deep_agent can bind them
        # (bind_tools on the model directly returns a RunnableBinding which isn't a BaseChatModel)
        if is_gemini:
            logger.info("Adding built-in tools for Gemini model: google_search, code_execution")
            static_tools_list = static_tools_list + [{"google_search": {}}, {"code_execution": {}}]

        system_prompt = (
            self.config.SYSTEM_INSTRUCTION_SHORT
            if os.environ.get("USE_SHORT_PROMPTS") == "true" and self.config.SYSTEM_INSTRUCTION_SHORT
            else self.config.SYSTEM_INSTRUCTION
        )
        compiled_graph = create_deep_agent(
            model=model,
            tools=static_tools_list,
            system_prompt=system_prompt,
            checkpointer=self._checkpointer,
            store=store_instance,  # Shared PostgreSQL document store (initialized)
            backend=backend,  # type: ignore[arg-type]
            middleware=middleware,  # type: ignore[arg-type]
            context_schema=GraphRuntimeContext,
            response_format=response_format,
        )
        # Override deepagents default recursion_limit (1000) with configured value
        # This prevents infinite loops from reaching the high default limit
        compiled_graph = compiled_graph.with_config({"recursion_limit": self.config.MAX_RECURSION_LIMIT})
        logger.info(f"Graph created for model: {model_type} with recursion_limit={self.config.MAX_RECURSION_LIMIT}")

        return compiled_graph

    def get_graph(
        self, model_type: ModelType | None = None, thinking_level: ThinkingLevel | None = None
    ) -> CompiledStateGraph:
        """Get or create a graph for the given model type.

        Args:
            model_type: The type of model (defaults to the resolved default model)

        Returns:
            CompiledStateGraph: The graph instance (cached or newly created)
        """
        effective_model: ModelType = model_type or require_default_model()

        cache_key = (effective_model, thinking_level)

        if cache_key in self._graphs:
            return self._graphs[cache_key]

        logger.info(f"Creating graph for model: {effective_model}, thinking_level={thinking_level}")
        graph = self._create_graph(effective_model, thinking_level)
        # Don't cache a graph built while the store is still resolving (transient cold-start):
        # it binds store=None permanently. A decided mode ("indexed"/"absent") is stable and
        # safe to cache; ensure_store_ready() busts the cache if "absent" later upgrades.
        if self._store_enabled and self._store_mode is None:
            logger.warning("Graph for %s built before document store is ready; not caching (will retry)", effective_model)
            return graph
        self._graphs[cache_key] = graph
        return graph

    def _create_task_scheduler_graph(self, model_type: ModelType) -> CompiledStateGraph:
        """Create a custom task scheduler agent graph with middleware.

        The task-scheduler agent has:
        - context_schema=GraphRuntimeContext for accessing tools at runtime
        - DynamicToolDispatchMiddleware for tool execution (scheduler + console tools from SYSTEM_TOOLS)
        - Common middleware stack for file handling, summarization, caching, etc.
        - Structured output via SubAgentResponseSchema for explicit task_state determination

        Unlike GP agent, task-scheduler does NOT use ToolsetSelectorMiddleware because it
        always needs the same fixed set of tools (scheduler_* and console_*). These tools
        are provided via SYSTEM_TOOLS in DynamicToolDispatchMiddleware, which bypasses the
        user's tool whitelist.

        Middleware ordering (first = outermost wrapper):
        1. DynamicToolDispatchMiddleware: Injects scheduler/console tools from SYSTEM_TOOLS,
           handles tool execution for MCP tools
        2-8. common_middleware_stack: FilesystemMiddleware, SummarizationMiddleware,
           LiteLLMPromptCachingMiddleware,
           PatchToolCallsMiddleware,
           ToolRetryMiddleware, RepeatedToolCallMiddleware, ToolSchemaCleaningMiddleware

        Args:
            model_type: The type of model (defaults to claude-3.7-sonnet)

        Returns:
            CompiledStateGraph: The compiled task-scheduler agent graph
        """
        from ..agents.task_scheduler import (
            TASK_SCHEDULER_SYSTEM_PROMPT,
        )

        model = self._get_or_create_model(model_type, thinking_level=None)

        backend = self.backend_factory

        # DynamicToolDispatchMiddleware without tool selection
        # - Injects scheduler/console tools from SYSTEM_TOOLS
        # - Handles tool execution for MCP tools not in ToolNode
        task_scheduler_dispatch = DynamicToolDispatchMiddleware(
            static_tools=[],
            skip_tool_injection=False,  # Inject tools from registry + SYSTEM_TOOLS
            agent_settings=self.config,
            cost_logger=self.cost_logger,
        )

        # Common middleware stack (file handling, summarization, caching, etc.)
        common_stack = build_common_middleware_stack(model, backend, add_docstore_hint=self.store is not None)
        middleware = [
            task_scheduler_dispatch,
            *common_stack,  # FilesystemMiddleware, SummarizationMiddleware, caching, retry, etc.
        ]

        # Get response_format for structured output (SubAgentResponseSchema)
        # Enables task-scheduler to explicitly set task_state
        static_tools_list = self.get_static_tools(with_response_tool=False)
        response_format = get_sub_agent_response_format(
            model_type=model_type,
            tools=static_tools_list,
            thinking_enabled=False,  # Task-scheduler doesn't use thinking
        )

        task_scheduler_graph = create_agent(
            model=model,
            tools=static_tools_list,
            system_prompt=TASK_SCHEDULER_SYSTEM_PROMPT + SUB_AGENT_PROTOCOL_ADDENDUM,
            middleware=middleware,  # type: ignore[arg-type]
            context_schema=GraphRuntimeContext,
            checkpointer=self._checkpointer,
            store=self.store,
            response_format=response_format,
        )

        task_scheduler_graph = task_scheduler_graph.with_config({"recursion_limit": self.config.MAX_RECURSION_LIMIT})
        logger.info(f"Task-scheduler graph created for model: {model_type}")

        return task_scheduler_graph

    def get_task_scheduler_graph(self, model_type: ModelType | None = None) -> CompiledStateGraph:
        """Get or create a custom task-scheduler graph for the given model type.

        Task-scheduler graphs are cached by model_type only (no thinking_level).
        They are created lazily on first request. And will be used as task_scheduler_graph_provider for
        the scheduler agent runnable.

        Args:
            model_type: The type of model (defaults to claude-3.7-sonnet)

        Returns:
            CompiledStateGraph: The task-scheduler graph instance (cached or newly created)
        """
        from ..agents.task_scheduler import DEFAULT_TASK_SCHEDULER_MODEL

        effective_model: ModelType = model_type or DEFAULT_TASK_SCHEDULER_MODEL
        cache_key = (effective_model, None)  # No thinking level for task-scheduler

        if cache_key in self._task_scheduler_graphs:
            return self._task_scheduler_graphs[cache_key]

        logger.info(f"Creating task-scheduler graph for model: {effective_model}")
        graph = self._create_task_scheduler_graph(effective_model)
        # See get_graph: don't cache a store-less graph built during a transient cold start.
        if self._store_enabled and self._store_mode is None:
            logger.warning("Task-scheduler graph for %s built before store is ready; not caching", effective_model)
            return graph
        self._task_scheduler_graphs[cache_key] = graph
        return graph
