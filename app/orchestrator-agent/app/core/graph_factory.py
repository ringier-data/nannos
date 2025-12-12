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
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.structured_output import AutoStrategy
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph.state import CompiledStateGraph
from langgraph_checkpoint_aws import DynamoDBSaver

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


def _create_final_response_tool() -> BaseTool:
    """Create the FinalResponseSchema tool for Bedrock models.

    Returns:
        StructuredTool for final response handling
    """

    def final_response_handler(**kwargs):
        """Handler for FinalResponseSchema tool - returns the structured response."""
        return FinalResponseSchema(**kwargs)

    return StructuredTool.from_function(
        func=final_response_handler,
        name="FinalResponseSchema",
        description=(
            "REQUIRED: You MUST call this tool to provide your final response. "
            "This tool signals task completion and determines the appropriate task state "
            "(completed, working, input_required, or failed). Call this after you've finished "
            "processing the user's request and determined the outcome."
        ),
        args_schema=FinalResponseSchema,
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
        self._static_tools_cache: dict[bool, list[BaseTool]] = {}

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
    def a2a_middleware(self) -> A2ATaskTrackingMiddleware:
        """Get the A2A task tracking middleware (needed by discovery service)."""
        return self._a2a_middleware

    def _create_model(self, model_type: ModelType) -> BaseChatModel:
        """Create a model instance for the given model type.

        Args:
            model_type: The type of model to create ('gpt4o' or 'claude-sonnet-4.5')

        Returns:
            BaseChatModel: The created model instance
        """
        return create_model(model_type, self.config, self.thinking)

    def _get_or_create_model(self, model_type: ModelType) -> BaseChatModel:
        """Get or create a model instance (cached).

        Args:
            model_type: The type of model ('gpt4o' or 'claude-sonnet-4.5')

        Returns:
            BaseChatModel: The model instance (cached or newly created)
        """
        if model_type not in self._models:
            logger.info(f"Creating model instance for: {model_type}")
            self._models[model_type] = self._create_model(model_type)
        return self._models[model_type]

    def _create_middleware_stack(self, is_bedrock: bool) -> list[Any]:
        """Create the complete middleware stack for a graph.

        Args:
            is_bedrock: Whether this is for a Bedrock model

        Returns:
            Complete middleware stack with DynamicToolDispatchMiddleware first
        """
        # Static tools available to all models (cached to avoid recreation)
        if is_bedrock not in self._static_tools_cache:
            static_tools: list[BaseTool] = []

            # Add time tool for current time and relative date calculations
            static_tools.append(create_time_tool())

            # Add presigned URL tool for dispatching files to sub-agents
            static_tools.append(create_presigned_url_tool())

            # Add FinalResponseSchema for Bedrock models
            if is_bedrock:
                static_tools.append(_create_final_response_tool())

            self._static_tools_cache[is_bedrock] = static_tools
        else:
            static_tools = self._static_tools_cache[is_bedrock]

        # DynamicToolDispatchMiddleware must be first to intercept model calls
        dynamic_tool_middleware = DynamicToolDispatchMiddleware(static_tools=static_tools)

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

    def get_static_tools(self, is_bedrock: bool = False) -> list[BaseTool]:
        """Get static tools for the given model type.

        Args:
            is_bedrock: Whether to get tools for Bedrock models (includes FinalResponseSchema)

        Returns:
            List of static tools (cached)
        """
        if is_bedrock not in self._static_tools_cache:
            # Trigger middleware creation to populate cache
            self._create_middleware_stack(is_bedrock)
        return self._static_tools_cache[is_bedrock]

    def _create_graph(self, model_type: ModelType) -> CompiledStateGraph:
        """Create a graph for the given model type.

        Args:
            model_type: The type of model ('gpt4o' or 'claude-sonnet-4.5')

        Returns:
            CompiledStateGraph: The newly created graph
        """
        model = self._get_or_create_model(model_type)
        is_bedrock = isinstance(model, ChatBedrockConverse)
        middleware = self._create_middleware_stack(is_bedrock)

        # Note: Sub-agents (both local and remote A2A) are now registered dynamically
        # via GraphRuntimeContext.subagent_registry at request time, not at graph creation.
        # This enables per-user sub-agent discovery and unified handling.

        if is_bedrock:
            logger.info(f"Creating Bedrock graph for model: {model_type}")
            compiled_graph = create_deep_agent(
                model=model,
                tools=[],  # Tools come from GraphRuntimeContext via middleware
                subagents=[],  # Sub-agents come from GraphRuntimeContext via middleware
                system_prompt=self.config.SYSTEM_INSTRUCTION,
                checkpointer=self._checkpointer,
                middleware=middleware,  # type: ignore
                context_schema=GraphRuntimeContext,
            )
        else:
            logger.info(f"Creating OpenAI graph for model: {model_type}")
            compiled_graph = create_deep_agent(
                model=model,
                tools=[],  # Tools come from GraphRuntimeContext via middleware
                subagents=[],  # Sub-agents come from GraphRuntimeContext via middleware
                system_prompt=self.config.SYSTEM_INSTRUCTION,
                checkpointer=self._checkpointer,
                middleware=middleware,  # type: ignore
                response_format=AutoStrategy(schema=FinalResponseSchema),
                context_schema=GraphRuntimeContext,
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
