"""
Graph factory for OrchestratorDeepAgent.

This module provides a centralized factory for creating and managing LangGraph instances.
It encapsulates all graph-related concerns:
- Model creation and caching (Bedrock vs OpenAI)
- Checkpointer setup (DynamoDB)
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
from agent_common.a2a.client_runnable import A2AClientRunnable as _ClientRunnable
from agent_common.a2a.structured_response import A2A_PROTOCOL_ADDENDUM as SUB_AGENT_PROTOCOL_ADDENDUM
from agent_common.a2a.structured_response import get_response_format as get_sub_agent_response_format
from agent_common.core.copy_file_tool import create_copy_file_tool
from agent_common.core.graph_utils import build_common_middleware_stack, create_indexing_backend_factory
from agent_common.core.model_factory import create_model, _has_aws_credentials
from agent_common.middleware.steering_middleware import SteeringMiddleware
from agent_common.middleware.storage_paths_middleware import StoragePathsInstructionMiddleware
from agent_common.models.base import DEFAULT_MODEL, ModelType, ThinkingLevel
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.structured_output import AutoStrategy, ToolStrategy
from langchain_aws.middleware.prompt_caching import BedrockPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from ringier_a2a_sdk.cost_tracking import CostLogger, CostTrackingCallback

from ..handlers import handle_auth_error, should_retry
from ..middleware import (
    A2ATaskTrackingMiddleware,
    AuthErrorDetectionMiddleware,
    DynamicToolDispatchMiddleware,
    RepeatedToolCallMiddleware,
    TodoStatusMiddleware,
    ToolsetSelectorMiddleware,
    UserPreferencesMiddleware,
)
from ..models.config import AgentSettings, GraphRuntimeContext
from ..models.schemas import FinalResponseSchema
from .file_tools import create_presigned_url_tool
from .steering_state import get_all_active_subagent_dispatches, get_orchestrator_pending_messages
from .time_tools import create_time_tool

logger = logging.getLogger(__name__)

# System prompt for the custom general-purpose agent graph.
# This agent is invoked when the orchestrator delegates a "general-purpose" task.
# It has access to MCP tools filtered by ToolsetSelectorMiddleware.
GP_SYSTEM_PROMPT = (
    "You are a helpful general-purpose assistant with access to a curated set of tools "
    "that have been selected as relevant to the current task. Use these tools to accomplish "
    "the user's request thoroughly and accurately. When you're done, provide a clear and "
    "complete summary of what was accomplished."
)


class GraphFactory:
    """Factory for creating and managing LangGraph instances.

    Centralizes all graph-related concerns:
    - Model creation/caching (Bedrock vs OpenAI)
    - Checkpointer (shared DynamoDB instance)
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
        self._gp_graphs: dict[tuple[str, str | None], CompiledStateGraph] = {}
        self._task_scheduler_graphs: dict[tuple[str, str | None], CompiledStateGraph] = {}

        # Static tools cache (created once per model type, reused)
        self._static_tools_cache: list[BaseTool] = []

        # Create shared checkpointer for all graphs
        # This enables conversation continuity when users switch models
        self._checkpointer: BaseCheckpointSaver = self._create_checkpointer(config)

        # Create shared document store with PostgreSQL + pgvector (optional)
        # When PostgreSQL is not configured, the store is None and the agent
        # falls back to ephemeral StateBackend (no persistent document storage).
        self._postgres_conn: str | None = None
        self._embeddings_model = None
        self._store_enabled = bool(config.POSTGRES_HOST and config.POSTGRES_PASSWORD)

        if self._store_enabled:
            self._postgres_conn = (
                f"postgresql://{config.POSTGRES_USER}:{config.POSTGRES_PASSWORD}"
                f"@{config.POSTGRES_HOST}:{config.POSTGRES_PORT}/{config.POSTGRES_DB}"
            )
            if _has_aws_credentials():
                from agent_common.core.cost_tracking_embeddings import CostTrackingBedrockEmbeddings

                self._embeddings_model = CostTrackingBedrockEmbeddings(
                    model_id="amazon.titan-embed-text-v2:0",
                    region_name=config.get_bedrock_region(),
                    cost_logger=self.cost_logger,
                )
                logger.debug("Configured AsyncPostgresStore with Bedrock embeddings (will initialize on first access)")
            else:
                logger.info(
                    "PostgreSQL configured but AWS credentials unavailable – "
                    "document store will work without semantic indexing"
                )
                logger.debug("Configured AsyncPostgresStore without embeddings (will initialize on first access)")
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
        self._loop_detection_middleware = RepeatedToolCallMiddleware(max_repeats=5, window_size=10)
        self._retry_middleware = ToolRetryMiddleware(
            max_retries=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_FACTOR,
            retry_on=should_retry,
            on_failure=handle_auth_error,
        )
        logger.debug("Initialized middleware stack")

    @staticmethod
    def _create_checkpointer(config: AgentSettings) -> BaseCheckpointSaver:
        """Create the appropriate checkpointer based on available infrastructure.

        Uses DynamoDB when AWS credentials and a table name are configured,
        otherwise falls back to an in-memory checkpointer for local development.
        """
        if _has_aws_credentials() and config.CHECKPOINT_DYNAMODB_TABLE_NAME:
            from langgraph_checkpoint_aws import DynamoDBSaver

            s3_config: dict[str, str] | None = None
            if config.CHECKPOINT_S3_BUCKET_NAME:
                s3_config = {"bucket_name": config.CHECKPOINT_S3_BUCKET_NAME}
                logger.info(f"S3 offloading enabled for large checkpoints: {config.CHECKPOINT_S3_BUCKET_NAME}")

            checkpointer = DynamoDBSaver(
                table_name=config.CHECKPOINT_DYNAMODB_TABLE_NAME,
                region_name=config.CHECKPOINT_AWS_REGION,
                ttl_seconds=config.CHECKPOINT_TTL_DAYS * 24 * 60 * 60,
                enable_checkpoint_compression=config.CHECKPOINT_COMPRESSION_ENABLED,
                s3_offload_config=s3_config,  # type: ignore[arg-type]
            )
            logger.info("Initialized shared DynamoDB checkpointer with S3 offloading support")
            return checkpointer

        from langgraph.checkpoint.memory import MemorySaver

        logger.info(
            "AWS credentials or CHECKPOINT_DYNAMODB_TABLE_NAME not configured – "
            "using in-memory checkpointer (conversations will not persist across restarts)"
        )
        return MemorySaver()

    @property
    def checkpointer(self) -> BaseCheckpointSaver:
        """Get the shared checkpointer instance."""
        return self._checkpointer

    @property
    def store(self):
        """Get the shared document store instance (or None when PostgreSQL is not configured).

        Lazy initialization: creates the store on first access.
        Note: Pool connections are created asynchronously on first use.

        IMPORTANT: Call ensure_store_setup() once before using the store for the first time.
        """
        if not self._store_enabled:
            return None

        if self._store is None:
            from langgraph.store.postgres.aio import AsyncPostgresStore
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            # Create connection pool for AsyncPostgresStore (create once and reuse)
            # Pool will be opened explicitly in ensure_store_setup()
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
                index_config = {
                    "dims": 1024,  # Titan Embeddings V2 dimension
                    "embed": self._embeddings_model,
                    "fields": ["contextualized_content"],
                }

            self._store = AsyncPostgresStore(
                conn=self._connection_pool,
                index=index_config,
            )
            if index_config:
                logger.info("Initialized AsyncPostgresStore with Titan Embeddings V2 (1024 dims) and connection pool")
            else:
                logger.info("Initialized AsyncPostgresStore without semantic indexing (no embeddings)")
        return self._store

    @property
    def backend_factory(self) -> Any:
        """Get the backend factory for FilesystemMiddleware.

        Returns a factory function that creates a ``CompositeBackend`` with:
        - Default: ``StateBackend`` (ephemeral storage in agent state)
        - ``/memories/``: ``IndexingStoreBackend`` (persistent storage with semantic indexing)

        Delegates to ``create_indexing_backend_factory`` from ``agent_common``.

        Returns:
            Callable ``(ToolRuntime) -> CompositeBackend``
        """
        bedrock_region = self.config.get_bedrock_region() if _has_aws_credentials() else None
        return create_indexing_backend_factory(
            self.store, bedrock_region, cost_logger=self.cost_logger
        )

    async def ensure_store_setup(self) -> None:
        """Ensure the database schema is set up for the document store.

        This method creates the necessary tables and indexes in PostgreSQL if they don't exist.
        It handles cleanup of incompatible existing schemas.
        It's safe to call multiple times - subsequent calls are no-ops.
        Should be called once when the application starts before using the store.

        No-op when PostgreSQL is not configured.
        """
        if not self._store_enabled:
            logger.info("Document store not configured – skipping schema setup")
            return

        if not self._store_setup_complete:
            store = self.store  # Access property to ensure store is initialized

            # Open connection pool if not already open
            if self._connection_pool is not None and not self._connection_pool._opened:
                await self._connection_pool.open()
                logger.info("Opened AsyncConnectionPool for document store")

            # Check if store table exists with incompatible schema and clean it up
            try:
                from langgraph.checkpoint.postgres import _ainternal

                async with _ainternal.get_connection(store.conn) as conn:
                    async with conn.cursor() as cur:
                        # Check if store table exists
                        await cur.execute("""
                            SELECT EXISTS (
                                SELECT FROM information_schema.tables 
                                WHERE table_name = 'store'
                            )
                        """)
                        table_exists = (await cur.fetchone())[0]

                        if table_exists:
                            # Check if prefix column exists
                            await cur.execute("""
                                SELECT EXISTS (
                                    SELECT FROM information_schema.columns 
                                    WHERE table_name = 'store' AND column_name = 'prefix'
                                )
                            """)
                            has_prefix = (await cur.fetchone())[0]

                            if not has_prefix:
                                # Drop incompatible table and related objects
                                logger.warning("Found incompatible 'store' table schema, dropping and recreating...")
                                await cur.execute("DROP TABLE IF EXISTS store CASCADE")
                                await cur.execute("DROP TABLE IF EXISTS store_migrations CASCADE")
                                await cur.execute("DROP TABLE IF EXISTS vector_migrations CASCADE")
                                await cur.execute("DROP TABLE IF EXISTS store_vectors CASCADE")
                                await conn.commit()
                                logger.info("Dropped incompatible schema objects")

            except Exception as e:
                logger.error(f"Error checking/cleaning store schema: {e}")
                # Continue anyway - setup() will handle it

            # Now run the standard setup
            await store.setup()
            self._store_setup_complete = True
            logger.info("AsyncPostgresStore schema setup completed successfully")

    @property
    def a2a_middleware(self) -> A2ATaskTrackingMiddleware:
        """Get the A2A task tracking middleware (needed by discovery service)."""
        return self._a2a_middleware

    async def close(self) -> None:
        """Close the connection pool and clean up resources.

        Should be called when the GraphFactory is no longer needed (e.g., on application shutdown).
        """
        # Shutdown cost logger if present
        if self.cost_logger is not None:
            await self.cost_logger.shutdown()
            logger.info("Cost logger shutdown complete")

        # Close database connection pool
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
        # Create callbacks list if cost_logger is available
        callbacks = []
        if self.cost_logger:
            callbacks.append(CostTrackingCallback(self.cost_logger))

        return create_model(
            model_type, self.config.get_bedrock_region(), thinking_level, callbacks=callbacks if callbacks else None
        )

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

    def _create_middleware_stack(self) -> list[Any]:
        """Create the complete middleware stack for a graph.

        Middleware Execution Order (LangChain convention):
          - before_* hooks: First to last (list order)
          - after_*  hooks: Last to first (reverse order)
          - wrap_*   hooks: Nested — first middleware in the list wraps all
            others (outermost).  For ``awrap_tool_call`` the call path is:
                [0] → [1] → … → [N] → ToolNode handler
            So [0] can short-circuit before any later middleware sees the call.

        IMPORTANT: ``DynamicToolDispatchMiddleware`` is deliberately first ([0])
        because it short-circuits the ``task`` tool for A2A sub-agent dispatch.
        Being outermost means inner middlewares (Auth, Retry, …) never see the
        ``task`` tool call — they only see the ToolMessage result if it was
        returned.  This is why ``AuthErrorDetectionMiddleware`` cannot intercept
        A2A 401 errors via ``interrupt()``.

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

        # StoragePathsInstructionMiddleware adds filesystem storage paths documentation
        storage_paths_middleware = StoragePathsInstructionMiddleware()

        # Outermost → innermost (for wrap_* hooks):
        # DynamicToolDispatch[0] → StoragePaths → PromptCaching → Steering
        # → UserPreferences → LoopDetection → Auth → Retry → A2A → Todo[9]
        #
        # BedrockPromptCaching places cache point after all static content (system prompt + storage paths),
        # so that the cache is shared across all users. StoragePaths is included in the cache.
        # Steering comes after caching so follow-up messages aren't cached.
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
                        role="user",
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

        return [
            dynamic_tool_middleware,
            storage_paths_middleware,
            BedrockPromptCachingMiddleware(),
            steering_middleware,
            user_preferences_middleware,
            self._loop_detection_middleware,
            self._auth_middleware,
            self._retry_middleware,
            self._a2a_middleware,
            self._todo_middleware,
        ]

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
                    description="ALWAYS use this tool to format your final response to the user.",
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
        # The general-purpose agent is a GPAgentRunnable (LocalA2ARunnable) registered
        # in subagent_registry as "general-purpose". It wraps a custom GP graph (created
        # by _create_gp_graph) that has:
        #   - context_schema=GraphRuntimeContext for accessing MCP tools at runtime
        #   - ToolsetSelectorMiddleware for smart Phase 1+2 tool filtering
        #   - DynamicToolDispatchMiddleware(skip_tool_injection=True) for tool execution
        #
        # The default GP from create_deep_agent is still created internally but never
        # invoked because DynamicToolDispatchMiddleware dispatches "general-purpose"
        # from subagent_registry (the GPAgentRunnable) before it can fall through.

        backend = self.backend_factory

        # Use ToolStrategy for OpenAI models (avoids .parse() API that requires strict tools)
        # Use AutoStrategy for Bedrock without extended thinking (efficient, handles structured output natively)
        # For Bedrock with extended thinking and Gemini models: use response_format=None and add
        # FinalResponseSchema as an explicit static tool. Gemini's AutoStrategy resolves to ToolStrategy
        # but the model embeds the structured JSON in text content instead of tool_call_chunks,
        # causing raw JSON to be streamed to the client. The explicit tool approach ensures proper
        # tool_call_chunks streaming detection.
        requires_response_tool = False
        if model_type in ("gpt-4o", "gpt-4o-mini", "gpt-5.4-mini", "gpt-5.4-nano", "local"):
            response_format = ToolStrategy(schema=FinalResponseSchema)
        elif model_type in ("claude-sonnet-4.5", "claude-sonnet-4.6", "claude-haiku-4-5"):
            # if thinking is enabled we need to set response_format to None since the bedrock api can't handle
            # forcing structured output when enabling thinking
            if thinking_level:
                response_format = None
                requires_response_tool = True
            else:
                response_format = AutoStrategy(schema=FinalResponseSchema)
        elif model_type in ("gemini-3.1-pro-preview", "gemini-3-flash-preview"):
            # Gemini models: use explicit FinalResponseSchema tool instead of AutoStrategy/ToolStrategy
            # because Gemini outputs structured JSON in content text rather than via tool_call_chunks
            response_format = None
            requires_response_tool = True
        else:
            response_format = AutoStrategy(schema=FinalResponseSchema)
        middleware = self._create_middleware_stack()
        static_tools_list = self.get_static_tools(with_response_tool=requires_response_tool)

        # Add Google built-in tools for Gemini models
        # These are passed via the tools parameter so create_deep_agent can bind them
        # (bind_tools on the model directly returns a RunnableBinding which isn't a BaseChatModel)
        if model_type in ("gemini-3.1-pro-preview", "gemini-3-flash-preview"):
            logger.info("Adding built-in tools for Gemini model: google_search, code_execution")
            static_tools_list = static_tools_list + [{"google_search": {}}, {"code_execution": {}}]

        system_prompt = self.config.SYSTEM_INSTRUCTION_SHORT if os.environ.get('USE_SHORT_PROMPTS') == 'true' and self.config.SYSTEM_INSTRUCTION_SHORT else self.config.SYSTEM_INSTRUCTION
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
            model_type: The type of model (defaults to DEFAULT_MODEL)

        Returns:
            CompiledStateGraph: The graph instance (cached or newly created)
        """
        effective_model: ModelType = model_type or DEFAULT_MODEL

        cache_key = (effective_model, thinking_level)

        if cache_key not in self._graphs:
            logger.info(f"Creating graph for model: {effective_model}, thinking_level={thinking_level}")
            self._graphs[cache_key] = self._create_graph(effective_model, thinking_level)
        return self._graphs[cache_key]

    def _create_gp_graph(self, model_type: ModelType, thinking_level: Optional[ThinkingLevel]) -> CompiledStateGraph:
        """Create a custom general-purpose agent graph with tool selection middleware.

        Unlike the built-in GP from deepagents (which can't be customized), this GP agent:
        - Has context_schema=GraphRuntimeContext for accessing MCP tools at runtime
        - Uses ToolsetSelectorMiddleware for smart tool filtering (Phase 1 + Phase 2)
        - Uses DynamicToolDispatchMiddleware(skip_tool_injection=True) for tool execution
        - Gets MCP tools from ToolsetSelectorMiddleware, not from static compilation
        - Uses FilesystemMiddleware + IndexingStoreBackend for semantic indexing of files
        - Uses SummarizationMiddleware to handle large context windows
        - Uses AnthropicPromptCachingMiddleware for prompt caching on Anthropic models
        - Uses BedrockPromptCachingMiddleware for prompt caching on Bedrock models
        - Uses PatchToolCallsMiddleware to normalise tool call format

        Middleware ordering (first = outermost wrapper):
        1. ToolsetSelectorMiddleware: reads ALL MCP tools from tool_registry,
           Phase 1 selects relevant MCP servers, Phase 2 selects individual tools.
           Both phases are cached across model calls within a single GP invocation.
        2. DynamicToolDispatchMiddleware(skip_tool_injection=True): converts BaseTool→dict
           for Gemini compatibility, but does NOT inject from tool_registry. Resolves
           dynamic (MCP) tools via request.override(tool=...) so the full inner chain
           executes for every tool call — no middleware inside it is bypassed.
        3-9. common_middleware_stack: FilesystemMiddleware, SummarizationMiddleware,
           AnthropicPromptCachingMiddleware, BedrockPromptCachingMiddleware,
           PatchToolCallsMiddleware,
           ToolRetryMiddleware, RepeatedToolCallMiddleware, ToolSchemaCleaningMiddleware.

        Args:
            model_type: The type of model
            thinking_level: Optional thinking level for the model

        Returns:
            CompiledStateGraph: The compiled GP agent graph
        """
        model = self._get_or_create_model(model_type, thinking_level)

        backend = self.backend_factory

        # ToolsetSelectorMiddleware: reads ALL MCP tools from tool_registry,
        # Phase 1 selects relevant servers, Phase 2 selects individual tools.
        # Both phases cached across model calls within a GP invocation.
        # Docstore tools are always included so the GP agent can read/write persistent
        # memory regardless of which MCP servers are active for the current task.
        toolset_selector = ToolsetSelectorMiddleware(
            always_include=[
                "get_current_time",
                "generate_presigned_url",
                "docstore_search",
                "read_personal_file",
                "docstore_export",
                "copy_file",
            ],
            cost_logger=self.cost_logger,
        )

        # DynamicToolDispatchMiddleware with skip_tool_injection=True:
        # - Does NOT inject tools from tool_registry (ToolsetSelectorMiddleware handles that)
        # - Does handle tool EXECUTION (awrap_tool_call) for MCP tools not in ToolNode
        gp_dynamic_dispatch = DynamicToolDispatchMiddleware(
            static_tools=[],
            skip_tool_injection=True,
            agent_settings=self.config,
            cost_logger=self.cost_logger,
        )

        # DynamicToolDispatchMiddleware now resolves dynamic (MCP) tools via
        # request.override(tool=...) and delegates to handler, so the full inner chain
        # runs for every tool call.  No hoisting of FilesystemMiddleware is needed.
        common_stack = build_common_middleware_stack(model, backend, add_docstore_hint=self.store is not None)
        middleware = [
            toolset_selector,
            gp_dynamic_dispatch,
            *common_stack,  # FilesystemMiddleware, SummarizationMiddleware, caching, retry, etc.
        ]

        # Get response_format for structured output (SubAgentResponseSchema)
        # This allows the GP agent to explicitly set task_state (completed/input_required/failed)
        # rather than always returning "completed".
        # Note: For Bedrock+thinking, this mutates static_tools_list by appending the response tool.
        static_tools_list = self.get_static_tools(with_response_tool=False)
        response_format = get_sub_agent_response_format(
            model=model,
            tools=static_tools_list,
            thinking_enabled=bool(thinking_level),
        )

        gp_graph = create_agent(
            model=model,
            tools=static_tools_list,
            system_prompt=GP_SYSTEM_PROMPT + SUB_AGENT_PROTOCOL_ADDENDUM,
            middleware=middleware,  # type: ignore[arg-type]
            context_schema=GraphRuntimeContext,
            checkpointer=self._checkpointer,
            store=self.store,
            response_format=response_format,
        )

        gp_graph = gp_graph.with_config({"recursion_limit": self.config.MAX_RECURSION_LIMIT})
        logger.info(f"GP graph created for model: {model_type}, thinking_level={thinking_level}")

        return gp_graph

    def get_gp_graph(
        self, model_type: ModelType | None = None, thinking_level: ThinkingLevel | None = None
    ) -> CompiledStateGraph:
        """Get or create a custom GP graph for the given model type.

        GP graphs are cached by (model_type, thinking_level) just like orchestrator graphs.
        They are created lazily on first request.

        Args:
            model_type: The type of model (defaults to DEFAULT_MODEL)
            thinking_level: Optional thinking level

        Returns:
            CompiledStateGraph: The GP graph instance (cached or newly created)
        """
        effective_model: ModelType = model_type or DEFAULT_MODEL
        cache_key = (effective_model, thinking_level)

        if cache_key not in self._gp_graphs:
            logger.info(f"Creating GP graph for model: {effective_model}, thinking_level={thinking_level}")
            self._gp_graphs[cache_key] = self._create_gp_graph(effective_model, thinking_level)
        return self._gp_graphs[cache_key]

    def _create_task_scheduler_graph(self, model_type: ModelType) -> CompiledStateGraph:
        """Create a custom task scheduler agent graph with middleware.

        The task-scheduler agent has:
        - context_schema=GraphRuntimeContext for accessing tools at runtime
        - DynamicToolDispatchMiddleware for tool execution (scheduler + playground tools from SYSTEM_TOOLS)
        - Common middleware stack for file handling, summarization, caching, etc.
        - Structured output via SubAgentResponseSchema for explicit task_state determination

        Unlike GP agent, task-scheduler does NOT use ToolsetSelectorMiddleware because it
        always needs the same fixed set of tools (scheduler_* and playground_*). These tools
        are provided via SYSTEM_TOOLS in DynamicToolDispatchMiddleware, which bypasses the
        user's tool whitelist.

        Middleware ordering (first = outermost wrapper):
        1. DynamicToolDispatchMiddleware: Injects scheduler/playground tools from SYSTEM_TOOLS,
           handles tool execution for MCP tools
        2-8. common_middleware_stack: FilesystemMiddleware, SummarizationMiddleware,
           AnthropicPromptCachingMiddleware, BedrockPromptCachingMiddleware,
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
        # - Injects scheduler/playground tools from SYSTEM_TOOLS
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
            model=model,
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

        if cache_key not in self._task_scheduler_graphs:
            logger.info(f"Creating task-scheduler graph for model: {effective_model}")
            self._task_scheduler_graphs[cache_key] = self._create_task_scheduler_graph(effective_model)
        return self._task_scheduler_graphs[cache_key]
