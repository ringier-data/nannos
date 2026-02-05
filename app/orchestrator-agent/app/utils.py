"""Shared utility functions for the orchestrator agent."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Language code to display name mapping
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sl": "Slovenian",
    "sr": "Serbian",
    "uk": "Ukrainian",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "he": "Hebrew",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "sw": "Swahili",
}


def get_language_display_name(language_code: str) -> str:
    """Get the display name for a language code.

    Args:
        language_code: ISO 639-1 language code (e.g., 'en', 'de', 'fr')

    Returns:
        Human-readable language name, or the code itself if not found
    """
    return LANGUAGE_NAMES.get(language_code.lower(), language_code)


# ============================================================================
# Tool Schema Cleaning for Gemini Compatibility
# ============================================================================
"""
CONTEXT: LangChain's tool conversion and Gemini's strict schema validation
===========================================================================

This section provides workarounds for a schema validation issue when using
LangChain tools with Google's Gemini models via langchain-google-genai.

THE ISSUE:
----------
Some LangChain tools (particularly those from deepagents like FilesystemMiddleware)
have parameters with:
1. None as the annotation type
2. None as the default value
3. Complex types like BaseStore that Pydantic can't serialize

When these tools are converted to OpenAI function format via convert_to_openai_tool(),
the resulting schema contains properties with None values, which Gemini's strict
validation rejects with:

    pydantic_core.ValidationError: 1 validation error for Schema
    properties.runtime
      Input should be a valid dictionary or object to extract fields from
      [type=model_attributes_type, input_value=None, input_type=NoneType]

OpenAI and Claude models accept these schemas, but Gemini does not.

THE SOLUTION:
-------------
Clean dict schemas at model-binding time (not at tool creation time):
  - Remove properties with None values
  - Remove empty dicts and {"default": None}
  - Remove properties containing any None values
  - Sync required array with cleaned properties

This is handled by middleware (DynamicToolDispatchMiddleware and ToolSchemaCleaningMiddleware)
which intercepts model calls and cleans the dict representations of tools before sending
them to the model, while keeping the original BaseTool instances intact for execution.

WHY AT MODEL-BINDING TIME:
--------------------------
- Cleaning at tool creation breaks tool execution (tools need their full schema)
- Cleaning at model-binding time allows us to send clean schemas to the model
  while keeping the original tools intact for ToolNode execution
- Prevents "tool not found" errors that occur when tool schemas are modified in-place

USAGE:
------
For orchestrator:
    # Handled automatically by DynamicToolDispatchMiddleware
    tool_dict = convert_to_openai_tool(tool)
    tool_dict = validate_and_clean_tool_dict(tool_dict)

For sub-agents:
    # Handled automatically by ToolSchemaCleaningMiddleware
    tools = [tool1, tool2, ...]  # Keep as BaseTool instances

TODO: This should be fixed upstream in langchain-google-genai or langchain-core
===========================================================================
"""


def clean_schema_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove invalid property schemas.

    Removes properties with None values, empty dicts, or None in their values
    from dict schemas. This ensures Gemini compatibility.

    Args:
        properties: Properties dict from JSON Schema

    Returns:
        Cleaned properties dict
    """
    if not isinstance(properties, dict):
        return properties

    cleaned = {}
    for key, value in properties.items():
        # Skip None values entirely
        if value is None:
            logger.debug(f"Removing property '{key}' with None value")
            continue

        # Skip empty dicts
        if isinstance(value, dict) and len(value) == 0:
            logger.debug(f"Removing property '{key}' with empty dict")
            continue

        # Skip dicts with only {"default": None}
        if isinstance(value, dict) and value == {"default": None}:
            logger.debug(f"Removing property '{key}' with default: None")
            continue

        # Skip if the dict contains any top-level None values
        if isinstance(value, dict) and any(v is None for k, v in value.items()):
            logger.debug(f"Removing property '{key}' containing None values: {value}")
            continue

        # Recursively clean nested schemas
        if isinstance(value, dict):
            value_copy = dict(value)
            if "properties" in value_copy:
                value_copy["properties"] = clean_schema_properties(value_copy["properties"])
            if "items" in value_copy and isinstance(value_copy["items"], dict) and "properties" in value_copy["items"]:
                value_copy["items"]["properties"] = clean_schema_properties(value_copy["items"]["properties"])
            cleaned[key] = value_copy
        else:
            cleaned[key] = value

    return cleaned


def validate_and_clean_tool_dict(tool_dict: dict[str, Any]) -> dict[str, Any]:
    """Validate and clean tool dict schema for Gemini compatibility.

    Ensures parameters has valid JSON Schema structure and cleans properties
    with None values.

    Args:
        tool_dict: Tool in OpenAI dict format

    Returns:
        Tool dict with validated and cleaned parameters schema
    """
    # Ensure function key exists
    if "function" not in tool_dict:
        tool_dict = {"function": tool_dict, "type": "function"}

    function_dict = tool_dict["function"]
    parameters = function_dict.get("parameters")

    # Ensure parameters has valid structure
    if parameters is None or not isinstance(parameters, dict):
        function_dict["parameters"] = {"type": "object", "properties": {}}
    elif "properties" not in parameters:
        parameters["properties"] = {}

    # Clean invalid properties and sync required array
    if "properties" in function_dict["parameters"]:
        original_props = function_dict["parameters"]["properties"]
        cleaned_props = clean_schema_properties(original_props)
        function_dict["parameters"]["properties"] = cleaned_props

        # Remove cleaned properties from required array
        if "required" in function_dict["parameters"]:
            function_dict["parameters"]["required"] = [
                r for r in function_dict["parameters"]["required"] if r in cleaned_props
            ]

    return tool_dict


def build_runtime_context(
    user_config: Any,  # UserConfig
    agent_settings: Any = None,
    oauth2_client: Any = None,
    checkpointer: Any = None,
    static_tools: list[Any] | None = None,
    document_store: Any = None,  # AsyncPostgresStore | None
    s3_service: Any = None,  # S3Service | None
    document_store_bucket: str | None = None,
    backend_factory: Any = None,
    cost_logger: Any = None,
    backend_url: str | None = None,
) -> Any:  # GraphRuntimeContext
    """Build GraphRuntimeContext from user config and orchestrator dependencies.

    Transforms discovered tools and subagents lists into registries
    for dynamic tool dispatch at runtime. Also includes:
    - Built-in local sub-agents (like file-analyzer)
    - Remote A2A agents from discovery
    - Dynamic local sub-agents from user configuration
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

    Returns:
        GraphRuntimeContext for graph invocation
    """
    # Import here to avoid circular dependencies
    from deepagents import CompiledSubAgent
    from ringier_a2a_sdk.cost_tracking import CostTrackingCallback

    from .a2a_utils.models import LocalFoundrySubAgentConfig, LocalLangGraphSubAgentConfig
    from .agents.dynamic_agent import create_dynamic_local_subagent
    from .agents.file_analyzer import create_file_analyzer_subagent
    from .agents.foundry_agent import create_foundry_local_subagent
    from .core.document_store_tools import create_document_store_tools
    from .core.model_factory import create_model, get_default_model, is_valid_model
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
                            agent_settings,
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
                            subagent_model_type, agent_settings, thinking_level=thinking_level_to_use
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
                        agent_settings=agent_settings,
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
    )
