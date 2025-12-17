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
from typing import Any, Optional

from deepagents import create_deep_agent
from deepagents.backends.composite import CompositeBackend, StateBackend
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.structured_output import AutoStrategy
from langchain_aws import BedrockEmbeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph_checkpoint_aws import DynamoDBSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..backends import IndexingStoreBackend
from ..handlers import handle_auth_error, should_retry
from ..middleware import (
    AuthErrorDetectionMiddleware,
    DynamicToolDispatchMiddleware,
    RepeatedToolCallMiddleware,
    TodoStatusMiddleware,
    UserPreferencesMiddleware,
)
from ..models import AgentSettings, FinalResponseSchema
from ..models.config import GraphRuntimeContext, ModelType
from ..subagents import A2ATaskTrackingMiddleware
from .file_tools import create_presigned_url_tool
from .model_factory import create_model
from .time_tools import create_time_tool

logger = logging.getLogger(__name__)

# Default model when none specified
DEFAULT_MODEL: ModelType = "claude-sonnet-4.5"


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
        factory = GraphFactory(config, thinking=False)
        graph = factory.get_graph("claude-sonnet-4.5")
        # Use graph.astream(..., context=runtime_context)
    """

    def __init__(
        self,
        config: AgentSettings,
        thinking: bool = False,
        a2a_middleware: Optional[A2ATaskTrackingMiddleware] = None,
    ):
        """Initialize the graph factory.

        Args:
            config: Agent settings with model config, checkpoint config, etc.
            thinking: Enable thinking mode for Claude models
            a2a_middleware: Optional A2A task tracking middleware (shared with discovery)
        """
        self.config = config
        self.thinking = thinking

        # Model and graph caches
        self._models: dict[str, BaseChatModel] = {}
        self._graphs: dict[str, CompiledStateGraph] = {}

        # Static tools cache (created once per model type, reused)
        self._static_tools_cache: list[BaseTool] = []

        # Create shared checkpointer for all graphs
        # This enables conversation continuity when users switch models
        # Uses langgraph-checkpoint-aws with S3 offloading for large checkpoints (>350KB)
        s3_config: dict[str, str] | None = None
        if config.CHECKPOINT_S3_BUCKET_NAME:
            s3_config = {"bucket_name": config.CHECKPOINT_S3_BUCKET_NAME}
            logger.info(f"S3 offloading enabled for large checkpoints: {config.CHECKPOINT_S3_BUCKET_NAME}")

        self._checkpointer = DynamoDBSaver(
            table_name=config.CHECKPOINT_DYNAMODB_TABLE_NAME,
            region_name=config.CHECKPOINT_AWS_REGION,
            ttl_seconds=config.CHECKPOINT_TTL_DAYS * 24 * 60 * 60,  # Convert days to seconds
            enable_checkpoint_compression=config.CHECKPOINT_COMPRESSION_ENABLED,
            s3_offload_config=s3_config,  # type: ignore[arg-type]
        )
        logger.info("Initialized shared DynamoDB checkpointer with S3 offloading support")

        # Create shared document store with PostgreSQL + pgvector
        # This enables semantic document storage and retrieval across all graphs
        # Store PostgreSQL connection details for lazy initialization
        # AsyncPostgresStore will be created when first accessed (requires event loop)
        self._postgres_conn = (
            f"postgresql://{config.POSTGRES_USER}:{config.POSTGRES_PASSWORD}"
            f"@{config.POSTGRES_HOST}:{config.POSTGRES_PORT}/{config.POSTGRES_DB}"
        )
        self._embeddings_model = BedrockEmbeddings(
            model_id="amazon.titan-embed-text-v2:0",
            region_name=config.get_bedrock_region(),
        )
        self._store: AsyncPostgresStore | None = None
        self._connection_pool: AsyncConnectionPool | None = None
        self._store_setup_complete: bool = False
        logger.debug("Configured AsyncPostgresStore (will initialize on first access)")

        # Create middleware instances (shared across all graphs)
        self._a2a_middleware = a2a_middleware or A2ATaskTrackingMiddleware()
        self._auth_middleware = AuthErrorDetectionMiddleware()
        self._todo_middleware = TodoStatusMiddleware()
        self._loop_detection_middleware = RepeatedToolCallMiddleware(max_repeats=3, window_size=10)
        self._retry_middleware = ToolRetryMiddleware(
            max_retries=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_FACTOR,
            retry_on=should_retry,
            on_failure=handle_auth_error,
        )
        logger.debug("Initialized middleware stack")

    @property
    def checkpointer(self) -> DynamoDBSaver:
        """Get the shared checkpointer instance."""
        return self._checkpointer

    @property
    def store(self) -> AsyncPostgresStore:
        """Get the shared document store instance.

        Lazy initialization: creates the store on first access.
        Note: Pool connections are created asynchronously on first use.

        IMPORTANT: Call ensure_store_setup() once before using the store for the first time.
        """
        if self._store is None:
            # Create connection pool for AsyncPostgresStore (create once and reuse)
            # Connections will be created asynchronously on first database operation
            if self._connection_pool is None:
                self._connection_pool = AsyncConnectionPool(
                    self._postgres_conn,
                    min_size=2,
                    max_size=10,
                    kwargs={
                        "autocommit": True,
                        "prepare_threshold": 0,
                        "row_factory": dict_row,
                    },
                )

            self._store = AsyncPostgresStore(
                conn=self._connection_pool,
                index={
                    "dims": 1024,  # Titan Embeddings V2 dimension
                    "embed": self._embeddings_model,
                    "fields": ["context_description", "content"],  # Fields to index for semantic search
                },
            )
            logger.info("Initialized AsyncPostgresStore with Titan Embeddings V2 (1024 dims) and connection pool")
        return self._store

    async def ensure_store_setup(self) -> None:
        """Ensure the database schema is set up for the document store.

        This method creates the necessary tables and indexes in PostgreSQL if they don't exist.
        It handles cleanup of incompatible existing schemas.
        It's safe to call multiple times - subsequent calls are no-ops.
        Should be called once when the application starts before using the store.
        """
        if not self._store_setup_complete:
            store = self.store  # Access property to ensure store is initialized

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

    def _create_model(self, model_type: ModelType) -> BaseChatModel:
        """Create a model instance for the given model type.

        Args:
            model_type: The type of model to create ('gpt4o', 'gpt-4o-mini', 'claude-sonnet-4.5', or 'claude-haiku-4-5')

        Returns:
            BaseChatModel: The created model instance
        """
        return create_model(model_type, self.config, self.thinking)

    def _get_or_create_model(self, model_type: ModelType) -> BaseChatModel:
        """Get or create a model instance (cached).

        Args:
            model_type: The type of model ('gpt4o', 'gpt-4o-mini', 'claude-sonnet-4.5', or 'claude-haiku-4-5')

        Returns:
            BaseChatModel: The model instance (cached or newly created)
        """
        if model_type not in self._models:
            logger.info(f"Creating model instance for: {model_type}")
            self._models[model_type] = self._create_model(model_type)
        return self._models[model_type]

    def _create_middleware_stack(self) -> list[Any]:
        """Create the complete middleware stack for a graph.


        Returns:
            Complete middleware stack with DynamicToolDispatchMiddleware first
        """
        # Static tools available to all models (cached to avoid recreation)
        if not self._static_tools_cache:
            static_tools: list[BaseTool] = []

            # Add time tool for current time and relative date calculations
            static_tools.append(create_time_tool())

            # Add presigned URL tool for dispatching files to sub-agents
            static_tools.append(create_presigned_url_tool())

            self._static_tools_cache = static_tools
        else:
            static_tools = self._static_tools_cache

        # DynamicToolDispatchMiddleware must be first to intercept model calls
        # Add Static tools directly to the graph (not via middleware) in case you want
        # FinalResponseSchema's return_direct=True to be respected by the model
        dynamic_tool_middleware = DynamicToolDispatchMiddleware(static_tools=[])

        # UserPreferencesMiddleware injects user preferences (language, etc.) into system prompt
        user_preferences_middleware = UserPreferencesMiddleware()

        # Order: DynamicToolDispatch → UserPreferences → LoopDetection → Auth → Retry → A2A → Todo
        # UserPreferences comes early to modify system prompt before other middleware
        # LoopDetection comes before Auth/Retry to catch loops early
        return [
            dynamic_tool_middleware,
            user_preferences_middleware,
            self._loop_detection_middleware,
            self._auth_middleware,
            self._retry_middleware,
            self._a2a_middleware,
            self._todo_middleware,
        ]

    def get_static_tools(self) -> list[BaseTool]:
        """Get static tools for the given model type.

        Returns:
            List of static tools (cached)
        """
        if not self._static_tools_cache:
            # Trigger middleware creation to populate cache
            self._create_middleware_stack()
        return self._static_tools_cache

    def _create_graph(self, model_type: ModelType) -> CompiledStateGraph:
        """Create a graph for the given model type.

        Args:
            model_type: The type of model ('gpt4o', 'gpt-4o-mini', 'claude-sonnet-4.5', or 'claude-haiku-4-5')

        Returns:
            CompiledStateGraph: The newly created graph
        """
        model = self._get_or_create_model(model_type)
        middleware = self._create_middleware_stack()

        # Ensure store is initialized before creating graph (required for longterm memory)
        # Access the store property to trigger lazy initialization
        store_instance = self.store

        # Note: Sub-agents (both local and remote A2A) are now registered dynamically
        # via GraphRuntimeContext.subagent_registry at request time, not at graph creation.
        # This enables per-user sub-agent discovery and unified handling.

        # Backend with automatic semantic indexing
        # IndexingStoreBackend handles /memories/* paths and automatically indexes
        # all written files for semantic search, including large tool results evicted
        # by FilesystemMiddleware
        def create_backend(rt: Any) -> CompositeBackend:
            return CompositeBackend(
                default=StateBackend(rt),
                routes={"/memories/": IndexingStoreBackend(rt, self.config)},
            )

        backend = create_backend
        static_tools_list = self.get_static_tools()
        compiled_graph = create_deep_agent(
            model=model,
            tools=static_tools_list,  # Include static tools with FinalResponseSchema
            subagents=[],  # Sub-agents come from GraphRuntimeContext via middleware
            system_prompt=self.config.SYSTEM_INSTRUCTION,
            checkpointer=self._checkpointer,
            store=store_instance,  # Shared PostgreSQL document store (initialized)
            backend=backend,  # type: ignore[arg-type]
            middleware=middleware,  # type: ignore[arg-type]
            context_schema=GraphRuntimeContext,
            response_format=AutoStrategy(schema=FinalResponseSchema),
        )
        # Override deepagents default recursion_limit (1000) with configured value
        # This prevents infinite loops from reaching the high default limit
        compiled_graph = compiled_graph.with_config({"recursion_limit": self.config.MAX_RECURSION_LIMIT})
        logger.info(f"Graph created for model: {model_type} with recursion_limit={self.config.MAX_RECURSION_LIMIT}")

        return compiled_graph

    def get_graph(self, model_type: ModelType | None = None) -> CompiledStateGraph:
        """Get or create a graph for the given model type.

        Args:
            model_type: The type of model (defaults to DEFAULT_MODEL)

        Returns:
            CompiledStateGraph: The graph instance (cached or newly created)
        """
        effective_model: ModelType = model_type or DEFAULT_MODEL

        if effective_model not in self._graphs:
            logger.info(f"Creating graph for model: {effective_model}")
            self._graphs[effective_model] = self._create_graph(effective_model)

        return self._graphs[effective_model]
