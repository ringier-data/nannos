"""Shared utility functions for the orchestrator agent."""

import logging
import os
from typing import Any

from agent_common.utils import (  # noqa: F401
    LANGUAGE_NAMES,
    get_language_display_name,
)

from app.models.config import AgentSettings, UserConfig

logger = logging.getLogger(__name__)


def build_runtime_context(
    user_config: UserConfig,
    agent_settings: AgentSettings | None = None,
    oauth2_client: Any = None,
    checkpointer: Any = None,
    static_tools: list[Any] | None = None,
    document_store: Any = None,  # AsyncPostgresStore | None
    s3_service: Any = None,  # S3Service | None
    document_store_bucket: str | None = None,
    backend_factory: Any = None,
    cost_logger: Any = None,
    backend_url: str | None = None,
    gp_graph_provider: Any = None,
    task_scheduler_graph_provider: Any = None,
) -> Any:  # GraphRuntimeContext
    """Build GraphRuntimeContext from user config and orchestrator dependencies.

    Transforms discovered tools and subagents lists into registries
    for dynamic tool dispatch at runtime. Also includes:
    - Built-in local sub-agents (like file-analyzer, task-scheduler)
    - Remote A2A agents from discovery
    - Dynamic local sub-agents from user configuration
    - General-purpose (GP) agent (if gp_graph_provider is provided)
    - Document store tools (if dependencies provided)

    Dynamic local sub-agents are instantiated with:
    - Essential orchestrator tools always included (get_current_time, docstore, etc.)
    - MCP tool discovery (lazy) if mcp_tools is a non-empty list
    - NO additional MCP tools if mcp_tools is None or empty list
    - Shared checkpointer for multi-turn conversation state
    - Shared document store for persistent memory (FilesystemMiddleware)
    - Shared backend factory for semantic indexing (IndexingStoreBackend)
    - Custom model selection via config.model_name (if specified)

    Args:
        user_config: User configuration with tools, sub-agents, and preferences.
        agent_settings: AgentSettings instance for model creation (required if local_subagents configured).
        oauth2_client: OAuth2 client for authenticated MCP discovery.
        checkpointer: Shared checkpointer for dynamic sub-agent multi-turn conversations.
        static_tools: Static tools from orchestrator (e.g., get_current_time, create_presigned_url).
        document_store: AsyncPostgresStore instance for document storage (optional).
        s3_service: S3Service for uploading direct retrieval results (optional).
        document_store_bucket: S3 bucket name for document store results (optional).
        backend_factory: Backend factory for FilesystemMiddleware (from GraphFactory).
        cost_logger: CostLogger instance for cost tracking callbacks (optional).
        backend_url: Backend URL for cost tracking (extracted from cost_logger if available).
        gp_graph_provider: Callable(model_type, thinking_level) -> CompiledStateGraph for GP agent.
        task_scheduler_graph_provider: Callable(model_type) -> CompiledStateGraph for task-scheduler agent.

    Returns:
        GraphRuntimeContext for graph invocation
    """
    # Import here to avoid circular dependencies
    from agent_common.a2a.models import LocalFoundrySubAgentConfig, LocalLangGraphSubAgentConfig
    from agent_common.agents.dynamic_agent import create_dynamic_local_subagent
    from agent_common.agents.foundry_agent import create_foundry_local_subagent
    from agent_common.core.document_store_tools import create_document_store_tools
    from agent_common.core.model_factory import create_model, get_default_model, is_valid_model
    from deepagents import CompiledSubAgent
    from ringier_a2a_sdk.cost_tracking import CostTrackingCallback

    from .agents.file_analyzer import create_file_analyzer_subagent
    from .agents.gp_agent import create_gp_local_subagent
    from .agents.task_scheduler import create_task_scheduler_subagent
    from .models.config import GraphRuntimeContext

    # Convert tools list to tool_registry (name -> tool mapping)
    tool_registry: dict[str, Any] = {}
    for tool in user_config.tools or []:
        if hasattr(tool, "name"):
            tool_registry[tool.name] = tool
        elif isinstance(tool, dict):
            tool_registry[tool.get("name", str(tool))] = tool

    # Add document store tools if dependencies are provided
    if document_store is not None and s3_service is not None and document_store_bucket:
        logger.info(f"Adding document store tools for user_id: {user_config.user_id}")

        doc_tools = create_document_store_tools(
            store=document_store,
            s3_service=s3_service,
            s3_bucket=document_store_bucket,
            user_id=user_config.user_id,  # Use database ID for docstore namespace
        )
        for tool in doc_tools:
            tool_registry[tool.name] = tool
        logger.info(f"Added {len(doc_tools)} document store tools: {[t.name for t in doc_tools]}")

    # Add catalog search tool if user has accessible catalogs
    logger.debug(
        f"User has {len(user_config.accessible_catalog_ids or [])} accessible catalogs for catalog_search tool"
    )
    if user_config.accessible_catalog_ids:
        from agent_common.core.catalog_tools import create_catalog_search_tool

        catalog_vector_bucket = os.environ.get("CATALOG_VECTOR_BUCKET_NAME", "")
        catalog_thumbnails_bucket = os.environ.get("CATALOG_THUMBNAILS_S3_BUCKET", "")
        if catalog_vector_bucket:
            catalog_tool = create_catalog_search_tool(
                accessible_catalog_ids=user_config.accessible_catalog_ids,
                thumbnails_s3_bucket=catalog_thumbnails_bucket,
                vector_bucket_name=catalog_vector_bucket,
                cost_logger=cost_logger,
            )
            if catalog_tool:
                tool_registry[catalog_tool.name] = catalog_tool
                logger.info(
                    "Added catalog_search tool for %d catalogs",
                    len(user_config.accessible_catalog_ids),
                )
        else:
            logger.warning("CATALOG_VECTOR_BUCKET_NAME not set, skipping catalog_search tool")

    # Start with built-in local sub-agents (like file-analyzer, task-scheduler)
    # These run in-process but use the same registry as remote A2A agents
    subagent_registry: dict[str, CompiledSubAgent] = {}
    subagent_registry["file-analyzer"] = create_file_analyzer_subagent(
        cost_logger=cost_logger,  # Share CostLogger from GraphFactory
        sub_agent_id=None,  # file-analyzer is a system service, not user-specific
        user_sub=user_config.user_sub,  # For cost attribution in file-analyzer
        # NOTE: With sub_agent_id=None, costs are attributed to the orchestrator.
        # This is intentional: file-analyzer is a built-in capability (not a user-created
        # sub-agent), so its costs are considered part of orchestrator overhead.
    )

    # Add task-scheduler sub-agent if graph provider is available
    # Task-scheduler requires a LangGraph graph with middleware for tool access
    if task_scheduler_graph_provider is not None:
        # Import here since we need GraphRuntimeContext which is defined in the calling module
        from .models.config import GraphRuntimeContext

        # Build task-scheduler's tool whitelist
        # Include specific tools needed for scheduling operations:
        # - All scheduler_* tools (validate, create, list, get, update, pause)
        # - Only specific console tools for sub-agent management and structured MCP tool discovery
        allowed_console_tools = {
            "console_list_sub_agents",
            # "console_create_sub_agent",  # The sub-agent normally can be created by the task-scheduler tool itself
            "console_update_sub_agent",  # TODO: in theory we should allow only to update automated sub-agents
            # MCP tool discovery - hierarchical navigation (servers -> tools -> details)
            "console_list_mcp_servers",  # List available MCP integration servers (GitHub, Jira, etc.)
            "console_grep_mcp_tools",  # Discover available MCP tools
        }
        task_scheduler_tool_names = [
            t.name
            for t in (user_config.tools or [])
            if t.name.startswith("scheduler_") or t.name in allowed_console_tools
        ]
        logger.debug(f"Task-scheduler tool whitelist: {task_scheduler_tool_names}")

        # We'll create a partial context just for task-scheduler registration
        # The full context will be passed at invocation time
        # IMPORTANT: Use tool_registry (not user_config.tools) to include document store tools
        task_scheduler_context = GraphRuntimeContext(
            user_id=user_config.user_id,
            user_sub=user_config.user_sub,
            name=user_config.name or "",
            email=user_config.email or "",
            tool_registry=tool_registry,  # Use full registry with docstore tools
            subagent_registry={},  # Empty for now, filled below
            whitelisted_tool_names=task_scheduler_tool_names,
        )

        subagent_registry["task-scheduler"] = create_task_scheduler_subagent(
            task_scheduler_graph_provider=task_scheduler_graph_provider,
            user_context=task_scheduler_context,
            model_type=user_config.model or get_default_model(),
            user_sub=user_config.user_sub,
            cost_logger=cost_logger,
        )

    # Add remote A2A sub-agents from discovery
    for subagent in user_config.sub_agents or []:
        if isinstance(subagent, dict) and "name" in subagent:
            subagent_registry[subagent["name"]] = subagent

    # Build whitelisted tool names for orchestrator
    # Start with backend registry (user_config.tool_names)
    whitelisted_tool_names = set(user_config.tool_names or [])

    # Auto-include scheduler tools and console tools (always available from MCP)
    # These are essential for the orchestrator to delegate to task-scheduler sub-agent
    allowed_orchestrator_tools = {
        "console_list_sub_agents",
        "console_update_sub_agent",
        "console_list_mcp_servers",
        "console_grep_mcp_tools",
    }
    orchestrator_auto_tools = {
        name for name in tool_registry.keys() if name.startswith("scheduler_") or name in allowed_orchestrator_tools
    }
    # Auto-include catalog_search — always available if user has accessible catalogs
    if "catalog_search" in tool_registry:
        orchestrator_auto_tools.add("catalog_search")
    whitelisted_tool_names.update(orchestrator_auto_tools)
    logger.debug(
        f"Whitelisted tools for orchestrator: {len(whitelisted_tool_names)} tools (including {len(orchestrator_auto_tools)} auto-included scheduler/console tools)"
    )

    # Add dynamic local sub-agents from user configuration
    # Requires agent_settings for model creation
    if user_config.local_subagents and agent_settings:
        # Combine user tools with static tools (like get_current_time, create_presigned_url)
        orchestrator_tools = list(tool_registry.values())
        if static_tools:
            # Filter out FinalResponseSchema from static tools - not needed for sub-agents
            filtered_static_tools = [t for t in static_tools if t.name != "FinalResponseSchema"]
            orchestrator_tools.extend(filtered_static_tools)
            tool_names = [t.name for t in filtered_static_tools]
            logger.info(
                f"Added {len(filtered_static_tools)} static tools to orchestrator_tools for sub-agents: {tool_names}"
            )
        orchestrator_model_type = user_config.model or get_default_model()

        for config in user_config.local_subagents:
            try:
                if isinstance(config, LocalFoundrySubAgentConfig):
                    # Create Foundry local sub-agent
                    dynamic_subagent = create_foundry_local_subagent(
                        config=config,
                        user={
                            "sub": user_config.user_sub,
                            "name": user_config.name,
                            "email": user_config.email,
                        },
                        backend_url=backend_url,
                        sub_agent_id=config.sub_agent_id,
                    )
                    subagent_registry[config.name] = dynamic_subagent
                    logger.info(f"Registered Foundry local sub-agent: {config.name}")
                    continue  # Skip to next config after Foundry creation
                elif isinstance(config, LocalLangGraphSubAgentConfig):
                    # Determine which model to use for this sub-agent
                    # If config.model_name is set, use it; otherwise inherit orchestrator model
                    if config.model_name:
                        # Validate and use the specified model
                        if is_valid_model(config.model_name):
                            subagent_model_type = config.model_name  # type: ignore
                            logger.info(f"Sub-agent '{config.name}' using custom model: {config.model_name}")
                        else:
                            logger.warning(
                                f"Sub-agent '{config.name}' has invalid model_name '{config.model_name}'. "
                                f"Falling back to orchestrator model: {orchestrator_model_type}"
                            )
                            subagent_model_type = orchestrator_model_type
                    else:
                        # Inherit orchestrator model
                        subagent_model_type = orchestrator_model_type
                        logger.debug(
                            f"Sub-agent '{config.name}' inheriting orchestrator model: {orchestrator_model_type}"
                        )

                    # Create the model for this sub-agent with cost tracking callbacks and thinking configuration
                    thinking_level_to_use = config.thinking_level if config.enable_thinking else None
                    if cost_logger:
                        # Create model with CostTrackingCallback for cost tracking
                        callbacks = [CostTrackingCallback(cost_logger)]
                        subagent_model = create_model(
                            subagent_model_type,
                            agent_settings.get_bedrock_region(),
                            thinking_level=thinking_level_to_use,
                            callbacks=callbacks,
                        )
                        logger.info(
                            f"Sub-agent '{config.name}' model created with cost tracking "
                            f"(model_type={subagent_model_type}, thinking_level={thinking_level_to_use})"
                        )
                    else:
                        # Fallback: create model without callbacks
                        subagent_model = create_model(
                            subagent_model_type,
                            agent_settings.get_bedrock_region(),
                            thinking_level=thinking_level_to_use,
                        )
                        logger.warning(
                            f"Sub-agent '{config.name}' model created WITHOUT cost tracking "
                            f"(model_type={subagent_model_type}, thinking_level={thinking_level_to_use})"
                        )

                    # Create dynamic sub-agent with orchestrator tools for inheritance
                    # (tools are overridden if config.mcp_gateway_url is set)
                    # Pass oauth2_client and user_token for authenticated MCP discovery
                    # Pass checkpointer for multi-turn conversation state
                    # Pass store and backend_factory for FilesystemMiddleware persistence
                    # Pass user preferences for personalized sub-agent behavior
                    dynamic_subagent = create_dynamic_local_subagent(
                        config=config,
                        model=subagent_model,
                        orchestrator_tools=orchestrator_tools,
                        oauth2_client=oauth2_client,
                        user_token=user_config.access_token.get_secret_value() if user_config.access_token else None,
                        checkpointer=checkpointer,
                        user_name=user_config.name,
                        user_language=user_config.language,
                        user_timezone=user_config.timezone,
                        custom_prompt=user_config.custom_prompt,
                        store=document_store,
                        backend_factory=backend_factory,
                        mcp_gateway_url=agent_settings.MCP_GATEWAY_URL if agent_settings else None,
                        mcp_gateway_client_id=agent_settings.MCP_GATEWAY_CLIENT_ID if agent_settings else None,
                    )
                    subagent_registry[config.name] = dynamic_subagent
                    logger.info(f"Registered dynamic local sub-agent: {config.name} (model: {subagent_model_type})")
                else:
                    logger.warning(f"Unknown local sub-agent type for '{config.name}': {type(config)}")
            except Exception as e:
                logger.error(f"Failed to create dynamic sub-agent '{config.name}': {e}")
                # Continue with other subagents (graceful degradation)
    elif user_config.local_subagents and not agent_settings:
        logger.warning(
            f"local_subagents configured but no agent_settings provided. "
            f"Skipping {len(user_config.local_subagents)} dynamic sub-agents."
        )

    logger.debug(f"Tool registry contains {len(tool_registry)} total tools")

    context = GraphRuntimeContext(
        user_id=user_config.user_id,  # Database ID (stable)
        user_sub=user_config.user_sub,  # OIDC sub (current)
        name=user_config.name,
        email=user_config.email,
        language=user_config.language,
        timezone=user_config.timezone,
        message_formatting=user_config.message_formatting,
        slack_user_handle=user_config.slack_user_handle,
        custom_prompt=user_config.custom_prompt,
        tool_registry=tool_registry,
        subagent_registry=subagent_registry,
        whitelisted_tool_names=whitelisted_tool_names,
    )

    # Register general-purpose (GP) agent as a local sub-agent.
    # This is done after GraphRuntimeContext creation because the GP agent needs
    # the context reference for runtime tool injection (context_schema=GraphRuntimeContext).
    # Since subagent_registry is a mutable dict, updating it here also updates the
    # reference inside context.
    if gp_graph_provider:
        model_type = user_config.model or get_default_model()
        thinking_level = user_config.thinking_level
        subagent_registry["general-purpose"] = create_gp_local_subagent(
            gp_graph_provider=gp_graph_provider,
            user_context=context,
            model_type=model_type,
            user_sub=user_config.user_sub,
            thinking_level=thinking_level,
            cost_logger=cost_logger,
        )
        logger.info(f"Registered GP local sub-agent (model: {model_type}, thinking: {thinking_level})")

    return context
