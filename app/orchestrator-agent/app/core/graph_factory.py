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
from langchain_openai import AzureChatOpenAI
from langgraph.graph.state import CompiledStateGraph
from langgraph_checkpoint_dynamodb import DynamoDBConfig, DynamoDBSaver, DynamoDBTableConfig

from ..handlers import handle_auth_error, should_retry
from ..middleware import (
    AuthErrorDetectionMiddleware,
    DynamicToolDispatchMiddleware,
    TodoStatusMiddleware,
    UserPreferencesMiddleware,
)
from ..models import AgentSettings, FinalResponseSchema
from ..models.config import GraphRuntimeContext, ModelType
from ..subagents import A2ATaskTrackingMiddleware
from .file_tools import create_presigned_url_tool

logger = logging.getLogger(__name__)

# Default model when none specified
DEFAULT_MODEL: ModelType = "gpt4o"


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

        # Create shared checkpointer for all graphs
        # This enables conversation continuity when users switch models
        self._checkpointer = DynamoDBSaver(
            DynamoDBConfig(
                table_config=DynamoDBTableConfig(
                    table_name=config.CHECKPOINT_TABLE_NAME,
                    ttl_days=config.CHECKPOINT_TTL_DAYS,
                ),
                region_name=config.CHECKPOINT_AWS_REGION,
                max_retries=config.CHECKPOINT_MAX_RETRIES,
            ),
            deploy=False,
        )
        logger.info("Initialized shared DynamoDB checkpointer")

        # Create middleware instances (shared across all graphs)
        self._a2a_middleware = a2a_middleware or A2ATaskTrackingMiddleware()
        self._auth_middleware = AuthErrorDetectionMiddleware()
        self._todo_middleware = TodoStatusMiddleware()
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
        if model_type == "claude-sonnet-4.5":
            if self.thinking:
                thinking_params = {"type": "enabled", "budget_tokens": 1024}
                temperature = 1.0
            else:
                thinking_params = {"type": "disabled", "budget_tokens": 0}
                temperature = 0.0

            return ChatBedrockConverse(
                model=self.config.get_bedrock_model_id(),
                temperature=temperature,
                region_name=self.config.get_bedrock_region(),
                additional_model_request_fields={"thinking": thinking_params}
                if thinking_params["type"] == "enabled"
                else {},
            )
        else:
            # Default to gpt4o (Azure OpenAI)
            if self.thinking:
                logger.warning("Thinking mode is only supported for Claude Sonnet 4.5 model.")

            return AzureChatOpenAI(
                azure_deployment=self.config.get_azure_deployment(),
                temperature=0.7,
                model=self.config.get_azure_model_name(),
            )

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
        # Static tools available to all models
        static_tools: list[BaseTool] = []

        # Add presigned URL tool for dispatching files to sub-agents
        static_tools.append(create_presigned_url_tool())

        # Add FinalResponseSchema for Bedrock models
        if is_bedrock:
            static_tools.append(_create_final_response_tool())

        # DynamicToolDispatchMiddleware must be first to intercept model calls
        dynamic_tool_middleware = DynamicToolDispatchMiddleware(static_tools=static_tools)

        # UserPreferencesMiddleware injects user preferences (language, etc.) into system prompt
        user_preferences_middleware = UserPreferencesMiddleware()

        # Order: DynamicToolDispatch → UserPreferences → Auth → Retry → A2A → Todo
        # UserPreferences comes early to modify system prompt before other middleware
        return [
            dynamic_tool_middleware,
            user_preferences_middleware,
            self._auth_middleware,
            self._retry_middleware,
            self._a2a_middleware,
            self._todo_middleware,
        ]

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

        logger.info(f"Graph created for model: {model_type}")
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
