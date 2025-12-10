"""Runtime context building utilities.

This module provides utilities for building GraphRuntimeContext from UserConfig.
Separated from config.py to avoid circular dependencies with model_factory.
"""

import logging
from typing import Any

from deepagents import CompiledSubAgent

from ..core.model_factory import ModelType, create_model
from ..subagents.dynamic_agent import create_dynamic_local_subagent
from ..subagents.file_analyzer import create_file_analyzer_subagent
from ..subagents.foundry_runnable import create_foundry_local_subagent
from ..subagents.models import LocalFoundrySubAgentConfig, LocalLangGraphSubAgentConfig
from .config import GraphRuntimeContext, UserConfig

logger = logging.getLogger(__name__)


def build_runtime_context(
    user_config: UserConfig,
    agent_settings: Any = None,
    oauth2_client: Any = None,
    checkpointer: Any = None,
    static_tools: list[Any] | None = None,
) -> GraphRuntimeContext:
    """Build GraphRuntimeContext from user config and orchestrator dependencies.

    Transforms discovered tools and subagents lists into registries
    for dynamic tool dispatch at runtime. Also includes:
    - Built-in local sub-agents (like file-analyzer)
    - Remote A2A agents from discovery
    - Dynamic local sub-agents from user configuration

    Dynamic local sub-agents are instantiated with:
    - Tools inherited from orchestrator if mcp_gateway_url is None
    - MCP tool discovery (lazy) if mcp_gateway_url is set
    - Shared checkpointer for multi-turn conversation state
    - Custom model selection via config.model_name (if specified)

    Args:
        user_config: User configuration with tools, sub-agents, and preferences.
        agent_settings: AgentSettings instance for model creation (required if local_subagents configured).
        oauth2_client: OAuth2 client for authenticated MCP discovery.
        checkpointer: Shared checkpointer for dynamic sub-agent multi-turn conversations.
        static_tools: Static tools from orchestrator (e.g., get_current_time, create_presigned_url).

    Returns:
        GraphRuntimeContext for graph invocation
    """
    # Convert tools list to tool_registry (name -> tool mapping)
    tool_registry: dict[str, Any] = {}
    for tool in user_config.tools or []:
        if hasattr(tool, "name"):
            tool_registry[tool.name] = tool
        elif isinstance(tool, dict):
            tool_registry[tool.get("name", str(tool))] = tool

    # Start with built-in local sub-agents (like file-analyzer)
    # These run in-process but use the same registry as remote A2A agents
    subagent_registry: dict[str, CompiledSubAgent] = {}
    subagent_registry["file-analyzer"] = create_file_analyzer_subagent()

    # Add remote A2A sub-agents from discovery
    for subagent in user_config.sub_agents or []:
        if isinstance(subagent, dict) and "name" in subagent:
            subagent_registry[subagent["name"]] = subagent

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
        orchestrator_model_type = user_config.model or "gpt4o"  # Default orchestrator model

        for config in user_config.local_subagents:
            try:
                if isinstance(config, LocalFoundrySubAgentConfig):
                    # Create Foundry local sub-agent
                    dynamic_subagent = create_foundry_local_subagent(
                        config=config,
                        user={
                            "sub": user_config.user_id,
                            "name": user_config.name,
                            "email": user_config.email,
                        },
                    )
                    subagent_registry[config.name] = dynamic_subagent
                    logger.info(f"Registered Foundry local sub-agent: {config.name}")
                    continue  # Skip to next config after Foundry creation
                elif isinstance(config, LocalLangGraphSubAgentConfig):
                    # Determine which model to use for this sub-agent
                    # If config.model_name is set, use it; otherwise inherit orchestrator model
                    subagent_model_type: ModelType
                    if config.model_name:
                        # Validate and use the specified model
                        if config.model_name in ["gpt4o", "claude-sonnet-4.5"]:
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

                    # Create the model for this sub-agent using the utility function
                    subagent_model = create_model(subagent_model_type, agent_settings)

                    # Create dynamic sub-agent with orchestrator tools for inheritance
                    # (tools are overridden if config.mcp_gateway_url is set)
                    # Pass oauth2_client and user_token for authenticated MCP discovery
                    # Pass checkpointer for multi-turn conversation state
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

    return GraphRuntimeContext(
        user_id=user_config.user_id,
        name=user_config.name,
        email=user_config.email,
        language=user_config.language,
        timezone=user_config.timezone,
        message_formatting=user_config.message_formatting,
        slack_user_handle=user_config.slack_user_handle,
        custom_prompt=user_config.custom_prompt,
        tool_registry=tool_registry,
        subagent_registry=subagent_registry,
    )
