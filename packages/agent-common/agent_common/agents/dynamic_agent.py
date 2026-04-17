"""Dynamic Local Agent - User-specific sub-agents with LangGraph execution.

This module provides a mechanism for dynamically provisioning user-specific
sub-agents that run in-process but communicate via the A2A protocol.

Unlike the file-analyzer (simple local agent) or remote A2A agents (network calls),
DynamicLocalAgentRunnable wraps a full LangGraph agent with:
- Custom system prompts (user-configurable)
- MCP tool discovery from Gatana gateway (lazy-loaded on first invocation)
- Optional tool whitelist filtering (mcp_tools from config)
- Standard A2A state responses (completed, input_required, failed)
- Structured output for explicit task state determination (no guessing)

Architecture:
- Inherits from LocalA2ARunnable for A2A protocol compliance
- Lazily creates a LangGraph agent on first invocation
- Always uses Gatana MCP gateway for tool discovery
- If config.mcp_tools is a non-empty list, filters discovered tools by that whitelist
- Otherwise (None or empty list), agent has NO MCP tools (only essential orchestrator tools)
- Uses structured output (SubAgentResponseSchema) for explicit task state

Use Case:
Users can configure personal sub-agents via playground backend with custom prompts and optional
tool whitelists, enabling specialized assistants without deploying separate A2A services.
"""

import logging
import os
from collections.abc import AsyncIterable
from typing import Any, Dict, List, Optional

from deepagents import CompiledSubAgent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_mcp_adapters.callbacks import Callbacks
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.store.postgres.aio import AsyncPostgresStore
from ringier_a2a_sdk.oauth import OidcOAuth2Client
from ringier_a2a_sdk.utils.mcp_errors import format_mcp_error, is_retryable_mcp_error
from ringier_a2a_sdk.utils.mcp_progress import on_mcp_progress
from ringier_a2a_sdk.utils.streaming import StreamBuffer, StructuredResponseStreamer, extract_text_from_content

from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from agent_common.a2a.models import LocalLangGraphSubAgentConfig
from agent_common.a2a.stream_events import (
    ActivityLogMeta,
    ArtifactUpdate,
    ErrorEvent,
    IntermediateOutputMeta,
    StreamEvent,
    TaskUpdate,
    WorkPlanMeta,
)
from agent_common.a2a.stream_utils import retrieve_final_state
from agent_common.a2a.structured_response import (
    A2A_PROTOCOL_ADDENDUM,
    StructuredResponseMixin,
    get_response_format,
)
from agent_common.core.graph_utils import build_sub_agent_graph
from agent_common.core.model_factory import get_model_input_capabilities
from agent_common.utils import get_language_display_name

logger = logging.getLogger(__name__)


def _validate_tool_schema(tool: BaseTool) -> BaseTool:
    """Validate and fix MCP tool schema for OpenAI API compatibility.

    OpenAI requires that if a 'parameters' field is present, it must be a valid
    JSON Schema object with a 'properties' field (even if empty). MCP tools
    sometimes have missing or invalid parameters schemas.

    This validation is critical for streaming SSE responses. If a tool schema
    is invalid, OpenAI returns a 400 error before streaming begins, causing
    the A2A server to return JSON instead of SSE, which breaks A2A clients
    expecting text/event-stream responses.

    This function modifies the tool's args_schema to ensure it has a valid
    parameters structure. Tools without args_schema or with invalid schemas
    are fixed by creating a minimal valid schema.

    Args:
        tool: BaseTool instance from MCP discovery

    Returns:
        Same tool instance (modified in place to fix schema)
    """
    # Import create_model for schema creation
    from pydantic import create_model

    # Check if tool has args_schema
    if not hasattr(tool, "args_schema") or tool.args_schema is None:
        # Create an empty args_schema
        tool.args_schema = create_model(f"{tool.name}Args")
        logger.debug(f"Tool '{tool.name}' had no args_schema, created empty schema")
        return tool

    # Verify the schema by converting to OpenAI format
    tool_dict = convert_to_openai_tool(tool)
    function_dict = tool_dict.get("function", {})
    parameters = function_dict.get("parameters")

    # If parameters is missing, not a dict, or missing properties field, fix it
    if parameters is None or not isinstance(parameters, dict) or "properties" not in parameters:
        # Create an empty args_schema to fix the tool
        tool.args_schema = create_model(f"{tool.name}Args")
        logger.warning(
            f"Tool '{tool.name}' had invalid parameters schema (missing properties field). Fixed with empty schema."
        )

    return tool


class DynamicLocalAgentRunnable(StructuredResponseMixin, LocalA2ARunnable):
    """A dynamically configured local sub-agent with full LangGraph capabilities.

    This runnable wraps a LangGraph agent that is lazily created on first invocation.
    It supports:
    - Custom system prompts from user configuration
    - MCP tool discovery from Gatana gateway (lazy-loaded)
    - Optional tool whitelist filtering (config.mcp_tools)
    - Inheritance of orchestrator tools when no whitelist is specified
    - Standard A2A protocol responses (completed, input_required, failed)

    The agent is created lazily to:
    1. Defer MCP tool discovery until the agent is actually used
    2. Allow tool inheritance from orchestrator context at invocation time
    3. Avoid resource consumption for agents that may never be called

    Attributes:
        config: LocalLangGraphSubAgentConfig with name, description, system_prompt, mcp_tools
        model: The LangGraph model to use for the agent
        orchestrator_tools: Essential tools always included (get_current_time, docstore, etc.)
        _agent: Lazily-created LangGraph agent (cached after first creation)
        _discovered_tools: Tools discovered from Gatana MCP gateway (cached after discovery)
    """

    def __init__(
        self,
        config: LocalLangGraphSubAgentConfig,
        model: BaseChatModel,
        orchestrator_tools: Optional[List[BaseTool]] = None,
        oauth2_client: Optional[OidcOAuth2Client] = None,
        user_token: Optional[str] = None,
        checkpointer: Optional[BaseCheckpointSaver] = None,
        user_name: Optional[str] = None,
        user_language: Optional[str] = None,
        user_timezone: Optional[str] = None,
        custom_prompt: Optional[str] = None,
        sub_agent_id: Optional[int] = None,
        store: Optional[AsyncPostgresStore] = None,
        backend_factory: Optional[Any] = None,
        mcp_gateway_url: Optional[str] = None,
        mcp_gateway_client_id: Optional[str] = None,
    ):
        """Initialize the dynamic local agent runnable.

        Args:
            config: Configuration with name, description, system_prompt, mcp_tools
            model: The LangGraph model to use for the agent
            orchestrator_tools: Essential tools always included (get_current_time, docstore, etc.)
            oauth2_client: OAuth2 client for token exchange (required for MCP tool discovery)
            user_token: User's access token for token exchange (required for MCP tool discovery)
            checkpointer: Shared checkpointer for multi-turn conversation state (e.g., DynamoDBSaver)
            user_name: User's display name for personalization
            user_language: User's preferred language (ISO 639-1 code)
            user_timezone: User's timezone (IANA timezone name)
            custom_prompt: User's custom prompt addendum
            sub_agent_id: Optional playground backend sub_agent ID for tracking agent-created agents
            store: Shared AsyncPostgresStore for document storage (enables FilesystemMiddleware persistence)
            backend_factory: Factory function for creating CompositeBackend (for FilesystemMiddleware)
            mcp_gateway_url: MCP gateway URL (defaults to MCP_GATEWAY_URL env var)
            mcp_gateway_client_id: MCP gateway client ID (defaults to MCP_GATEWAY_CLIENT_ID env var)
        """
        self.config = config
        self.model = model
        self.orchestrator_tools = orchestrator_tools or []
        self.oauth2_client = oauth2_client
        self.user_token = user_token
        self.sub_agent_id = sub_agent_id or config.sub_agent_id
        self.checkpointer = checkpointer
        self.user_name = user_name
        self.user_language = user_language
        self.user_timezone = user_timezone
        self.custom_prompt = custom_prompt
        self.store = store
        self.backend_factory = backend_factory
        self.mcp_gateway_url = mcp_gateway_url or os.getenv("MCP_GATEWAY_URL", "")
        self.mcp_gateway_client_id = mcp_gateway_client_id or os.getenv("MCP_GATEWAY_CLIENT_ID", "gatana")
        self._agent = None
        self._discovered_tools: Optional[List[BaseTool]] = None

    @property
    def name(self) -> str:
        """Return the agent name from configuration."""
        return self.config.name

    @property
    def description(self) -> str:
        """Return the agent description from configuration."""
        return self.config.description

    def get_supported_input_modes(self) -> List[str]:
        """Get list of input modes supported by this dynamic agent.

        Returns input modes from configuration if explicitly specified,
        otherwise derives capabilities from the model type. Falls back to
        ["text", "image"] if model type is unknown.

        Returns:
            List of supported content types (e.g., ["text", "image"])
        """
        # Use config if explicitly specified
        if self.config.input_modes:
            return self.config.input_modes
        # Derive from model capabilities if model is known
        model_type = self.get_model_type()
        if model_type:
            try:
                return get_model_input_capabilities(model_type)  # type: ignore[arg-type]
            except ValueError:
                pass
        # Default to text+image for modern models
        return ["text", "image"]

    def get_model_type(self) -> str | None:
        """Return the model type for provider-specific content transforms."""
        return self.config.model_name

    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        """Return checkpoint namespace for this dynamic agent."""
        return f"dynamic-{self.name}"

    def get_thread_id(self, context_id: str, input_data: SubAgentInput) -> str:
        """Build thread_id for checkpoint isolation.

        Pattern: {context_id}::dynamic-{agent_name}
        """
        return f"{context_id}::dynamic-{self.name}" if context_id else f"dynamic-{self.name}"

    def get_checkpointer(self, input_data: SubAgentInput) -> Optional[Any]:
        """Return DynamoDB checkpointer for this dynamic agent."""
        return self.checkpointer

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        """Return identifier for cost tracking.

        Uses playground sub_agent_id if available, otherwise falls back to dynamic-{name}.
        """
        if self.sub_agent_id:
            return str(self.sub_agent_id)
        return f"dynamic-{self.name}"

    def _build_preferences_addendum(self) -> str:
        """Build the user preferences addendum for the system prompt.

        TODO: this could also be configured per sub-agent in the future.

        Uses the same logic as UserPreferencesMiddleware to ensure consistency
        between orchestrator and sub-agent behavior.

        Returns:
            Formatted string to append to the system prompt
        """
        preferences_parts: List[str] = []

        # Language preference
        if self.user_language:
            language_name = get_language_display_name(self.user_language)
            preferences_parts.append(
                f"- **Response Language**: You MUST respond in {language_name} ({self.user_language}). "
                f"All your responses, explanations, and communications with the user should be in {language_name}. "
                f"However, technical terms, code, tool names, and API calls should remain in their original form."
            )

        # Timezone preference
        if self.user_timezone:
            preferences_parts.append(
                f"- **User Timezone**: The user's timezone is {self.user_timezone}. "
                f"When using the get_current_time tool, pass timezone='{self.user_timezone}' to get times in their local timezone."
            )

        # Custom prompt addendum from user settings
        if self.custom_prompt:
            preferences_parts.append(f"- **Custom Instructions**: {self.custom_prompt}")

        if not preferences_parts:
            return ""

        addendum = "\n\n**User Preferences:**\n" + "\n".join(preferences_parts)

        logger.debug(
            f"DynamicLocalAgentRunnable: Built preferences addendum for {self.name}: "
            f"language={self.user_language}, timezone={self.user_timezone}, "
            f"custom_prompt={'set' if self.custom_prompt else 'none'}"
        )

        return addendum

    async def _discover_mcp_tools(self) -> List[BaseTool]:
        """Discover tools from Gatana MCP gateway with authentication.

        This is called lazily on first invocation if config.mcp_tools is set.
        Uses the same token exchange flow as the orchestrator's ToolDiscoveryService
        to authenticate with the MCP gateway.

        If config.mcp_tools is set, only those tools are returned (whitelist filtering).
        The discovered tools override orchestrator tools entirely when whitelist is specified.

        Implements retry logic with exponential backoff for transient errors (502, 503, 504).

        Returns:
            List of discovered BaseTool instances (filtered by whitelist if specified)

        Raises:
            Exception: If MCP discovery fails (will result in failed state)
        """
        import asyncio

        mcp_gateway_url = self.mcp_gateway_url
        mcp_gateway_client_id = self.mcp_gateway_client_id

        logger.info(f"Discovering MCP tools for {self.name} from MCP gateway at {mcp_gateway_url}")

        # Retry parameters
        max_retries = 3
        initial_delay = 1.0
        last_error = None
        delay = initial_delay

        for attempt in range(max_retries):
            try:
                # Build connection headers - use token exchange if oauth2_client is available
                headers: dict[str, str] = {}

                if self.oauth2_client and self.user_token:
                    # Exchange user token for MCP gateway token (same flow as ToolDiscoveryService)
                    logger.debug(f"Exchanging token for MCP gateway access for {self.name}")
                    mcp_gateway_token = await self.oauth2_client.exchange_token(
                        subject_token=self.user_token,
                        target_client_id=mcp_gateway_client_id,
                        requested_scopes=["openid", "profile", "offline_access"],
                    )
                    headers["Authorization"] = f"Bearer {mcp_gateway_token}"
                    logger.info(f"Successfully exchanged token for MCP gateway ({self.name})")
                else:
                    logger.warning(
                        f"No OAuth2 client or user token available for {self.name}. "
                        f"MCP discovery may fail if authentication is required."
                    )

                client = MultiServerMCPClient(
                    connections={
                        mcp_gateway_client_id: StreamableHttpConnection(
                            transport="streamable_http",
                            url=mcp_gateway_url,
                            headers=headers if headers else None,
                        )
                    },
                    callbacks=Callbacks(on_progress=on_mcp_progress),
                )

                tools = await client.get_tools()
                logger.info(f"Discovered {len(tools)} MCP tools for {self.name}")

                tools = [tool for tool in tools if tool.name in (self.config.mcp_tools or [])]
                logger.info(f"Filtered to {len(tools)} tools based on whitelist for {self.name}")

                # Validate tool schemas to prevent OpenAI API errors
                validated_tools = [_validate_tool_schema(tool) for tool in tools]

                if attempt > 0:
                    logger.info(f"Successfully discovered MCP tools for {self.name} on attempt {attempt + 1}")
                return validated_tools

            except Exception as e:
                last_error = e

                # Check if this is a retryable error
                is_retryable = is_retryable_mcp_error(e)

                if not is_retryable or attempt >= max_retries - 1:
                    # Non-retryable error or exhausted retries
                    if is_retryable:
                        error_msg = format_mcp_error(e)
                        logger.error(
                            f"Failed to discover MCP tools for {self.name} after {attempt + 1} attempts: {error_msg}"
                        )
                    else:
                        logger.error(f"Non-retryable error discovering MCP tools for {self.name}: {e}")
                    raise

                # Retryable error - wait and retry
                logger.warning(
                    f"Transient error discovering MCP tools for {self.name} (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff

        # Should never reach here, but just in case
        raise last_error or Exception(f"Failed to discover MCP tools for {self.name}")

    def _get_effective_tools(self) -> List[BaseTool]:
        """Get the effective tools for this agent.

        Logic:
        - If mcp_tools is a non-empty list: use discovered tools + essential orchestrator tools
        - Otherwise (None or empty list): only essential orchestrator tools (NO MCP tools)

        Essential tools always included:
        - get_current_time: For temporal awareness
        - docstore_search: For semantic search over indexed documents
        - read_personal_file: For accessing personal workspace files
        - docstore_export: For exporting files to S3
        - create_presigned_url: For creating S3 presigned URLs

        All tools are validated to ensure they have proper OpenAI schema format.

        Returns:
            List of tools to use for the agent
        """
        # Essential orchestrator tools (always included)
        essential_tool_names = [
            "get_current_time",
            "docstore_search",
            "read_personal_file",
            "docstore_export",
            "create_presigned_url",
        ]
        essential_tools = [tool for tool in self.orchestrator_tools if tool.name in essential_tool_names]

        # Check if config.mcp_tools is a non-empty list
        if self.config.mcp_tools and len(self.config.mcp_tools) > 0:
            # Non-empty list means use discovered tools + essential orchestrator tools
            essential_tool_names_found = [tool.name for tool in essential_tools]
            logger.info(
                f"Using MCP tools + essential orchestrator tools for '{self.name}': "
                f"{len(self._discovered_tools or [])} MCP tools + {len(essential_tools)} essential tools {essential_tool_names_found}"
            )
            return (self._discovered_tools or []) + essential_tools

        # mcp_tools is None or empty list - only essential tools (NO MCP tools)
        essential_tool_names_found = [tool.name for tool in essential_tools]
        logger.info(
            f"No MCP tools configured for '{self.name}' - using only essential orchestrator tools: "
            f"{len(essential_tools)} tools {essential_tool_names_found}"
        )
        return [_validate_tool_schema(tool) for tool in essential_tools]

    async def _ensure_agent(self) -> Any:
        """Ensure the LangGraph agent is created (lazy initialization).

        On first call:
        1. If mcp_tools is set, discover tools from Gatana gateway with whitelist filtering
        2. Otherwise, use orchestrator tools (inheritance)
        3. Create the LangGraph agent with structured output for task state

        The agent uses SubAgentResponseSchema to explicitly determine task state,
        following the same pattern as the orchestrator's FinalResponseSchema.

        Returns:
            The compiled LangGraph agent
        """
        if self._agent is not None:
            return self._agent

        # Discover MCP tools if whitelist is configured (lazy, first invocation only)
        # Note: Only discover if mcp_tools is a non-empty list
        if self.config.mcp_tools and len(self.config.mcp_tools) > 0 and self._discovered_tools is None:
            self._discovered_tools = await self._discover_mcp_tools()

        tools = self._get_effective_tools()
        logger.info(f"Creating LangGraph agent '{self.name}' with {len(tools)} tools")

        # Build system prompt with A2A protocol addendum and user preferences
        system_prompt = self.config.system_prompt + A2A_PROTOCOL_ADDENDUM
        preferences_addendum = self._build_preferences_addendum()
        if preferences_addendum:
            system_prompt += preferences_addendum
            logger.debug(f"Added user preferences addendum to {self.name} system prompt")

        # Get model-specific response_format strategy (may mutate tools list for Bedrock+thinking)
        response_format = get_response_format(
            model=self.model,
            tools=tools,
            thinking_enabled=bool(self.config.thinking_level),
        )

        # Build agent via the shared helper: handles backend factory selection
        # (injected vs. auto-created), middleware stack assembly, and graph creation.
        self._agent = build_sub_agent_graph(
            model=self.model,
            tools=tools,
            system_prompt=system_prompt,
            checkpointer=self.checkpointer,  # Shared checkpointer for multi-turn conversations
            store=self.store,  # Shared document store for persistent memory
            response_format=response_format,
            backend_factory=self.backend_factory or None,
        )

        return self._agent

    async def _astream_impl(self, input_data: SubAgentInput, config: Dict[str, Any]) -> AsyncIterable[StreamEvent]:
        """Stream dynamic agent execution with real-time status updates and content.

        Streams the internal LangGraph execution to provide progress visibility for:
        - MCP tool invocations
        - Multi-step reasoning
        - Long-running operations
        - Incremental content delivery via artifact_update events

        Args:
            input_data: Validated input with messages and tracking IDs
            config: Extended config with checkpoint isolation and cost tracking

        Yields:
            Status updates and content chunks matching middleware expectations:
            - {\"type\": \"task_update\", \"state\": \"working\", ...} for status/activity
            - {\"type\": \"artifact_update\", \"content\": \"...\"} for streaming content
            - Terminal result in final yield

        Raises:
            ValueError: If context_id missing from input
            GraphInterrupt: If user intervention needed
        """
        # Prepare input with multi-modal support (handles content blocks)
        human_message = await self._prepare_human_message_input(input_data)
        context_id, task_id = self._extract_tracking_ids(input_data)

        try:
            # Ensure agent is created (lazy initialization)
            agent = await self._ensure_agent()

            agent_input = {"messages": [human_message]}

            # CRITICAL: Dynamic agent graphs are standalone, not subgraphs.
            # checkpoint_ns must be "" for standalone graphs (same pattern as GPAgentRunnable).
            # Thread isolation is already provided by unique thread_id="{context_id}::dynamic-{name}".
            standalone_config = {
                **config,
                "configurable": {
                    **config.get("configurable", {}),
                    "checkpoint_ns": "",  # Empty for standalone graph (not a subgraph)
                },
            }

            logger.debug(
                f"[COST TRACKING] Dynamic agent '{self.name}' streaming with tags: {standalone_config.get('tags', [])} "
                f"(thread_id={standalone_config.get('configurable', {}).get('thread_id')})"
            )

            # Shared streaming helpers
            response_streamer = StructuredResponseStreamer("SubAgentResponseSchema")
            stream_buffer = StreamBuffer()
            emitted_tool_calls: set[str] = set()  # Track tool calls to avoid duplicates

            # Stream the agent with custom events and messages using v2 format
            # v2: every chunk is a StreamPart dict: {"type": ..., "ns": ..., "data": ...}
            async for part in agent.astream(
                agent_input,
                config=standalone_config,
                stream_mode=["custom", "messages"],
                version="v2",
            ):
                # Extract working-state messages from intermediate updates
                status_text = None
                part_type = part["type"]

                # Capture tool calls and stream content from message chunks
                if part_type == "messages":
                    msg_chunk, _metadata = part["data"]
                    if not isinstance(msg_chunk, AIMessageChunk):
                        continue

                    # --- Tool call detection for activity log + structured response streaming ---
                    if msg_chunk.tool_call_chunks:
                        for tc_chunk in msg_chunk.tool_call_chunks:
                            tool_name = tc_chunk.get("name")
                            # Emit status for tool calls (excluding response schemas)
                            if (
                                tool_name
                                and tool_name not in ("FinalResponseSchema", "SubAgentResponseSchema")
                                and tool_name not in emitted_tool_calls
                            ):
                                emitted_tool_calls.add(tool_name)
                                yield TaskUpdate(
                                    status_text=f"Using {tool_name}\u2026",
                                    event_metadata=ActivityLogMeta(),
                                )
                            # Incremental structured response streaming
                            delta = response_streamer.feed(tc_chunk)
                            if delta:
                                stream_buffer.append(delta)
                                for chunk in stream_buffer.flush_ready():
                                    yield ArtifactUpdate(content=chunk)
                        continue

                    # --- Regular content streaming ---
                    if msg_chunk.content:
                        token_text, thinking_blocks = extract_text_from_content(msg_chunk.content)
                        for tb in thinking_blocks:
                            yield ArtifactUpdate(
                                content=tb["thinking"],
                                event_metadata=IntermediateOutputMeta(),
                            )
                        if token_text:
                            stream_buffer.append(token_text)
                            for chunk in stream_buffer.flush_ready():
                                yield ArtifactUpdate(content=chunk)
                    continue

                if part_type == "custom":
                    event_data = part["data"]
                    if isinstance(event_data, tuple) and len(event_data) == 2:
                        event_type, payload = event_data
                        if isinstance(payload, dict):
                            # Forward work plan updates from the sub-agent graph
                            if event_type == "todo_status" and "todos" in payload:
                                yield TaskUpdate(
                                    event_metadata=WorkPlanMeta(todos=payload["todos"]),
                                )
                                continue
                            status_text = payload.get("status")
                    elif isinstance(event_data, dict):
                        status_text = event_data.get("status")

                # Yield working-state status updates
                if status_text:
                    yield TaskUpdate(
                        status_text=status_text,
                        event_metadata=ActivityLogMeta(),
                    )

            # Flush remaining buffer
            remaining = stream_buffer.flush_all()
            if remaining:
                yield ArtifactUpdate(content=remaining)

            # Retrieve final state (checkpointer saves it after each node)
            final_values = retrieve_final_state(agent, standalone_config)
            result = self._translate_agent_result(final_values, context_id, task_id)

            # Yield terminal result
            # TODO: shall we enforce a state here?
            yield TaskUpdate(
                data=result,
            )

        except GraphInterrupt as gi:
            logger.info(f"[DYNAMIC AGENT] Graph interrupted during streaming in '{self.name}': {gi}")
            raise

        except Exception as e:
            logger.exception(f"Error streaming dynamic agent '{self.name}': {e}")

            # Check if this is an MCP discovery error
            error_msg = str(e)
            if "MCP" in error_msg or "tool" in error_msg.lower():
                error_result = self._build_error_response(
                    f"Failed to initialize agent tools: {e}",
                    context_id=context_id,
                    task_id=task_id,
                )
            else:
                error_result = self._build_error_response(
                    f"Agent execution error: {e}",
                    context_id=context_id,
                    task_id=task_id,
                )

            yield ErrorEvent(
                error=error_msg,
                data=error_result,
            )

    # _translate_agent_result and _build_response_from_schema are provided by StructuredResponseMixin


def create_dynamic_local_subagent(
    config: LocalLangGraphSubAgentConfig,
    model: BaseChatModel,
    orchestrator_tools: Optional[List[BaseTool]] = None,
    oauth2_client: Optional[OidcOAuth2Client] = None,
    user_token: Optional[str] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    user_name: Optional[str] = None,
    user_language: Optional[str] = None,
    user_timezone: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    store: Optional[AsyncPostgresStore] = None,
    backend_factory: Optional[Any] = None,
    mcp_gateway_url: Optional[str] = None,
    mcp_gateway_client_id: Optional[str] = None,
) -> CompiledSubAgent:
    """Create a dynamic local sub-agent from configuration.

    Factory function that creates a CompiledSubAgent wrapping a DynamicLocalAgentRunnable.
    This can be registered in the orchestrator's subagent_registry for use with the task tool.

    Args:
        config: LocalLangGraphSubAgentConfig with name, description, system_prompt, mcp_tools
        model: The LangGraph model to use for the agent
        orchestrator_tools: Essential tools always included (get_current_time, docstore, etc.)
        oauth2_client: OAuth2 client for token exchange (required for MCP tool discovery)
        user_token: User's access token for token exchange (required for MCP tool discovery)
        checkpointer: Shared checkpointer for multi-turn conversation state (e.g., DynamoDBSaver)
        user_name: User's display name for personalization
        user_language: User's preferred language (ISO 639-1 code)
        user_timezone: User's timezone (IANA timezone name)
        custom_prompt: User's custom prompt addendum
        store: Shared AsyncPostgresStore for document storage (enables FilesystemMiddleware persistence)
        backend_factory: Factory function for creating CompositeBackend (for FilesystemMiddleware)
        mcp_gateway_url: MCP gateway URL (defaults to MCP_GATEWAY_URL env var)
        mcp_gateway_client_id: MCP gateway client ID (defaults to MCP_GATEWAY_CLIENT_ID env var)

    Returns:
        CompiledSubAgent that can be registered with the orchestrator

    Example:
        config = LocalLangGraphSubAgentConfig(
            name="data-analyst",
            description="Analyzes data and generates insights",
            system_prompt="You are a data analysis expert...",
            mcp_tools=["query_database", "generate_chart"],  # Whitelist specific tools
        )
        subagent = create_dynamic_local_subagent(config, model, orchestrator_tools)
        subagent_registry["data-analyst"] = subagent
    """
    runnable = DynamicLocalAgentRunnable(
        config=config,
        model=model,
        orchestrator_tools=orchestrator_tools,
        oauth2_client=oauth2_client,
        user_token=user_token,
        checkpointer=checkpointer,
        user_name=user_name,
        user_language=user_language,
        user_timezone=user_timezone,
        custom_prompt=custom_prompt,
        sub_agent_id=config.sub_agent_id,
        store=store,
        backend_factory=backend_factory,
        mcp_gateway_url=mcp_gateway_url,
        mcp_gateway_client_id=mcp_gateway_client_id,
    )

    return CompiledSubAgent(
        name=config.name,
        description=config.description,
        runnable=runnable,  # type: ignore[arg-type]
    )
