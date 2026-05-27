"""Shared utility functions for the orchestrator agent."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_common.core.sandbox_pool import SandboxPool
    from agent_common.core.tool_risk_cache import ToolRiskCache
    from agent_common.middleware.conditional_hitl import RiskScorerFn

from agent_common.utils import (  # noqa: F401
    LANGUAGE_NAMES,
    get_language_display_name,
)

from app.models.config import AgentSettings, UserConfig

logger = logging.getLogger(__name__)


def _get_risk_scorer() -> RiskScorerFn:
    """Get the risk scorer function from agent-common (lazy import)."""
    from agent_common.core.tool_risk_scorer import score_tool_risk

    return score_tool_risk


def _wrap_tool_with_agent_name(tool: Any, agent_name: str) -> Any:
    """Wrap an MCP tool to default agent_name if not provided by the LLM.

    The agent_name field stays visible in the schema so the orchestrator can
    target a specific sub-agent. If the LLM omits it, it defaults to the
    orchestrator's own name.
    """
    from langchain_core.tools import BaseTool, StructuredTool

    if not isinstance(tool, BaseTool):
        return tool

    original_coroutine = getattr(tool, "coroutine", None)
    if not original_coroutine:
        return tool

    async def wrapped_coroutine(**kwargs: Any) -> Any:
        if kwargs.get("agent_name") in (None, "self"):
            kwargs["agent_name"] = agent_name
        return await original_coroutine(**kwargs)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=wrapped_coroutine,
        metadata=tool.metadata,
    )


def build_runtime_context(
    user_config: UserConfig,
    agent_settings: AgentSettings | None = None,
    oauth2_client: Any = None,
    checkpointer: Any = None,
    static_tools: list[Any] | None = None,
    document_store: Any = None,  # AsyncPostgresStore | None
    storage: Any = None,  # IObjectStorageService | None
    document_store_bucket: str | None = None,
    backend_factory: Any = None,
    cost_logger: Any = None,
    backend_url: str | None = None,
    task_scheduler_graph_provider: Any = None,
    sandbox_pool: SandboxPool | None = None,
    tool_risk_cache: ToolRiskCache | None = None,
) -> Any:  # GraphRuntimeContext
    """Build GraphRuntimeContext from user config and orchestrator dependencies.

    Transforms discovered tools and subagents lists into registries
    for dynamic tool dispatch at runtime. Also includes:
    - Built-in local sub-agents (like file-analyzer, task-scheduler)
    - Remote A2A agents from discovery
    - Dynamic local sub-agents from user configuration
    - General-purpose (GP) agent (loaded from DB as a DynamicLocalAgentRunnable)
    - Document store tools (if dependencies provided)

    Dynamic local sub-agents are instantiated with:
    - Essential orchestrator tools always included (get_current_time, docstore, etc.)
    - MCP tool discovery (lazy) if mcp_tools is a non-empty list
    - NO additional MCP tools if mcp_tools is None or empty list
    - Shared checkpointer for multi-turn conversation state
    - Shared document store for persistent memory (FilesystemMiddleware)
    - Shared backend factory for semantic indexing (IndexingStoreBackend)
    - Custom model selection via config.model_name (if specified)

    The GP agent is special:
    - Gets ALL tools from tool_registry (via inject_all_tools)
    - Has ToolsetSelectorMiddleware for smart LLM-driven tool filtering
    - Shares the same DynamicLocalAgentRunnable code path as other local agents

    Args:
        user_config: User configuration with tools, sub-agents, and preferences.
        agent_settings: AgentSettings instance for model creation (required if local_subagents configured).
        oauth2_client: OAuth2 client for authenticated MCP discovery.
        checkpointer: Shared checkpointer for dynamic sub-agent multi-turn conversations.
        static_tools: Static tools from orchestrator (e.g., get_current_time, create_presigned_url).
        document_store: AsyncPostgresStore instance for document storage (optional).
        storage: IObjectStorageService for uploading direct retrieval results (optional).
        document_store_bucket: Bucket name for document store results (optional).
        backend_factory: Backend factory for FilesystemMiddleware (from GraphFactory).
        cost_logger: CostLogger instance for cost tracking callbacks (optional).
        backend_url: Backend URL for cost tracking (extracted from cost_logger if available).
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
    from langchain_core.tools import BaseTool
    from ringier_a2a_sdk.cost_tracking import CostTrackingCallback

    from .agents.file_analyzer import create_file_analyzer_subagent
    from .agents.task_scheduler import create_task_scheduler_subagent
    from .middleware import ToolsetSelectorMiddleware
    from .models.config import GraphRuntimeContext

    # Convert tools list to tool_registry (name -> tool mapping)
    tool_registry: dict[str, Any] = {}
    for tool in user_config.tools or []:
        if hasattr(tool, "name"):
            tool_registry[tool.name] = tool
        elif isinstance(tool, dict):
            tool_registry[tool.get("name", str(tool))] = tool

    # Add document store tools if dependencies are provided
    if document_store is not None and storage is not None and document_store_bucket:
        logger.info(f"Adding document store tools for user_id: {user_config.user_id}")

        doc_tools = create_document_store_tools(
            store=document_store,
            storage=storage,
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
        "console_create_bug_report",
        "console_create_skill",
        "console_update_skill",
        "console_remove_skill",
        "console_update_playbook",
        "console_write_skill_file",
        "console_delete_skill_file",
        "console_search_skills",
        "console_import_skill",
        "console_activate_skill",
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

    # Default agent_name="orchestrator" for skill management tools so the LLM
    # doesn't need to provide it when operating on itself. The LLM can still
    # override agent_name to target a specific sub-agent.
    _SKILL_TOOLS_NEEDING_AGENT_NAME = {
        "console_create_skill",
        "console_update_skill",
        "console_remove_skill",
        "console_update_playbook",
        "console_write_skill_file",
        "console_delete_skill_file",
        "console_import_skill",
        "console_activate_skill",
    }
    for tool_name in _SKILL_TOOLS_NEEDING_AGENT_NAME:
        if tool_name in tool_registry:
            tool_registry[tool_name] = _wrap_tool_with_agent_name(tool_registry[tool_name], "orchestrator")

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
                    if config.sub_agent_id is not None:
                        dynamic_subagent["sub_agent_id"] = config.sub_agent_id  # type: ignore[typeddict-unknown-key]
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

                    # GP agent gets special treatment: all MCP tools injected directly
                    # + ToolsetSelectorMiddleware for smart LLM-driven filtering
                    gp_extra_middlewares = None
                    gp_inject_all_tools = None
                    if config.name == "general-purpose":
                        # Inject ALL MCP tools from tool_registry (already discovered by orchestrator)
                        gp_inject_all_tools = [t for t in tool_registry.values() if isinstance(t, BaseTool)]
                        # Add ToolsetSelectorMiddleware for smart tool filtering
                        gp_extra_middlewares = [
                            ToolsetSelectorMiddleware(
                                always_include=[
                                    "get_current_time",
                                    "generate_presigned_url",
                                    "docstore_search",
                                    "read_personal_file",
                                    "docstore_export",
                                    "copy_file",
                                ],
                                cost_logger=cost_logger,
                            ),
                        ]
                        logger.info(
                            f"GP agent: injecting {len(gp_inject_all_tools)} tools from tool_registry "
                            f"with ToolsetSelectorMiddleware"
                        )

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
                        user_id=user_config.user_id,
                        group_ids=user_config.groups if user_config.groups else None,
                        sandbox_pool=sandbox_pool if getattr(config, "sandbox_enabled", False) else None,
                        extra_middlewares=gp_extra_middlewares,
                        inject_all_tools=gp_inject_all_tools,
                        risk_scorer=_get_risk_scorer(),
                        tool_risk_cache=tool_risk_cache,
                        tool_bypass_rules=user_config.tool_bypass_rules,
                        pending_bypass_rules=user_config._pending_bypass_rules,  # will be updated during execution if user approves any bypasses
                    )
                    # Log if sandbox_enabled but no pool configured
                    if getattr(config, "sandbox_enabled", False) and not sandbox_pool:
                        logger.warning(
                            "Sub-agent '%s' has sandbox_enabled=true but no SANDBOX_PROVIDER configured; "
                            "running without sandbox (scripts readable but not executable)",
                            config.name,
                        )
                    subagent_registry[config.name] = dynamic_subagent
                    if config.sub_agent_id is not None:
                        dynamic_subagent["sub_agent_id"] = config.sub_agent_id  # type: ignore[typeddict-unknown-key]
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

    # Build tool_server_map from tool metadata (tool_name -> server_slug)
    tool_server_map: dict[str, str] = {}
    for tool_name, tool in tool_registry.items():
        metadata = getattr(tool, "metadata", None)
        if metadata and isinstance(metadata, dict):
            server_name = metadata.get("server_name")
            if server_name:
                tool_server_map[tool_name] = server_name
        # In-process tools without server_name fall back to "_self" in middleware

    context = GraphRuntimeContext(
        user_id=user_config.user_id,  # Database ID (stable)
        user_sub=user_config.user_sub,  # OIDC sub (current)
        name=user_config.name,
        email=user_config.email,
        language=user_config.language,
        timezone=user_config.timezone,
        message_formatting=user_config.message_formatting,
        client_user_handle=user_config.client_user_handle,
        custom_prompt=user_config.custom_prompt,
        groups=user_config.groups,
        tool_registry=tool_registry,
        subagent_registry=subagent_registry,
        whitelisted_tool_names=whitelisted_tool_names,
        tool_risk_cache=tool_risk_cache,
        tool_bypass_rules=user_config.tool_bypass_rules,
        tool_server_map=tool_server_map,
        user_system_role=user_config.user_system_role,
    )

    return context
