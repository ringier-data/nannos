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
from typing import Any, Dict, List, Literal, Optional

from deepagents import CompiledSubAgent
from deepagents.backends.composite import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.structured_output import AutoStrategy, ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.store.postgres.aio import AsyncPostgresStore
from pydantic import BaseModel, Field
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..a2a_utils.base import LocalA2ARunnable, SubAgentInput
from ..a2a_utils.models import LocalLangGraphSubAgentConfig
from ..middleware import RepeatedToolCallMiddleware
from ..utils import clean_tools_for_gemini, get_language_display_name

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


class SubAgentResponseSchema(BaseModel):
    """Structured output schema for sub-agent responses.

    Sub-agents MUST use this schema to explicitly indicate their task state.
    This eliminates guessing based on message content patterns.
    """

    task_state: Literal["completed", "input_required", "failed"] = Field(
        description=(
            "The task state for this response:\n"
            "- completed: Task finished successfully, provide summary of what was done\n"
            "- input_required: Need more information from user to proceed, ask a clear question\n"
            "- failed: Encountered an error that prevents completion, explain what went wrong"
        )
    )

    message: str = Field(
        description=(
            "The response message to send back:\n"
            "- For 'completed': Summary of what was accomplished\n"
            "- For 'input_required': Clear question asking for the specific information needed\n"
            "- For 'failed': Explanation of the error and any possible remediation"
        )
    )


# System prompt addendum that instructs the agent to use structured output
A2A_PROTOCOL_ADDENDUM = """
**Response Protocol:**
You are a sub-agent communicating with an orchestrator. You MUST determine the appropriate task state:

1. **completed** - Use when you have successfully completed the requested task. Provide a clear summary of what was accomplished.

2. **input_required** - Use when you need additional information from the user to proceed. Ask a specific, clear question about what information you need.

3. **failed** - Use when you encounter an error that prevents you from completing the task. Explain what went wrong and any potential remediation steps.

IMPORTANT: You must explicitly choose one of these states for every response. Do not leave the task state ambiguous.
"""


class DynamicLocalAgentRunnable(LocalA2ARunnable):
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
        agent_settings: Optional[Any] = None,
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
            agent_settings: AgentSettings for IndexingStoreBackend (enables semantic indexing)
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
        self.agent_settings = agent_settings
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

        Returns:
            List of discovered BaseTool instances (filtered by whitelist if specified)

        Raises:
            Exception: If MCP discovery fails (will result in failed state)
        """
        # Gatana MCP gateway URL (hardcoded - no longer configurable per sub-agent)
        mcp_gateway_url = "https://alloych.gatana.ai/mcp"

        logger.info(f"Discovering MCP tools for {self.name} from Gatana gateway")

        try:
            # Build connection headers - use token exchange if oauth2_client is available
            headers: dict[str, str] = {}

            if self.oauth2_client and self.user_token:
                # Exchange user token for MCP gateway token (same flow as ToolDiscoveryService)
                logger.debug(f"Exchanging token for MCP gateway access for {self.name}")
                mcp_gateway_token = await self.oauth2_client.exchange_token(
                    subject_token=self.user_token,
                    target_client_id="mcp-gateway",
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
                    "gatana": StreamableHttpConnection(
                        transport="streamable_http",
                        url=mcp_gateway_url,
                        headers=headers if headers else None,
                    )
                }
            )

            tools = await client.get_tools()
            logger.info(f"Discovered {len(tools)} MCP tools for {self.name}")

            tools = [tool for tool in tools if tool.name in (self.config.mcp_tools or [])]
            logger.info(f"Filtered to {len(tools)} tools based on whitelist for {self.name}")

            # Validate tool schemas to prevent OpenAI API errors
            validated_tools = [_validate_tool_schema(tool) for tool in tools]
            return validated_tools

        except Exception as e:
            logger.error(f"Failed to discover MCP tools for {self.name}: {e}")
            raise

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

        # WORKAROUND: LangChain bug - Some tools (particularly deepagents FilesystemMiddleware)
        # have parameters with None annotations/defaults that Gemini's strict validation rejects.
        # OpenAI and Claude accept these schemas, but Gemini does not.
        # See app/utils.py (Tool Schema Cleaning section) for detailed explanation and solution.
        tools = clean_tools_for_gemini(tools)

        # Build system prompt with A2A protocol addendum and user preferences
        system_prompt = self.config.system_prompt + A2A_PROTOCOL_ADDENDUM
        preferences_addendum = self._build_preferences_addendum()
        if preferences_addendum:
            system_prompt += preferences_addendum
            logger.debug(f"Added user preferences addendum to {self.name} system prompt")

        # Create backend for FilesystemMiddleware
        # Use orchestrator's backend factory if provided, otherwise fallback to simple StateBackend
        if self.backend_factory:
            backend = self.backend_factory
            logger.debug(f"Using orchestrator backend factory for {self.name}")
        else:
            # Fallback to simple StateBackend (ephemeral only, no persistence)
            backend = lambda rt: StateBackend(rt)
            logger.debug(f"Using simple StateBackend for {self.name} (no persistence)")

        # Create middleware list with FilesystemMiddleware
        middleware_list = [
            FilesystemMiddleware(backend=backend),
            ToolRetryMiddleware(
                max_retries=5,
                backoff_factor=2.0,
            ),
            RepeatedToolCallMiddleware(max_repeats=5, window_size=10),
        ]

        # Use ToolStrategy for OpenAI models (avoids .parse() API that requires strict tools)
        # Use AutoStrategy for Bedrock models (more efficient)
        if self.model.__class__.__name__ == "AzureChatOpenAI":
            response_format = ToolStrategy(schema=SubAgentResponseSchema)
        else:
            response_format = AutoStrategy(schema=SubAgentResponseSchema)

        self._agent = create_agent(
            self.model,
            system_prompt=system_prompt,
            tools=tools,
            checkpointer=self.checkpointer,  # Shared checkpointer for multi-turn conversations
            store=self.store,  # Shared document store for persistent memory
            response_format=response_format,
            middleware=middleware_list,
        )

        return self._agent

    async def _process(
        self,
        input_data: SubAgentInput,
    ) -> Dict[str, Any]:
        """Process the input using the LangGraph agent.

        Creates the agent lazily on first call, then invokes it with the content.
        Translates the agent's response into A2A protocol format.

        Args:
            input_data: The complete sub-agent input with content, IDs, and tracking

        Returns:
            Dict with 'messages' and A2A metadata (state, is_complete, etc.)

        Note on Checkpointing:
            Dynamic sub-agents use the SHARED orchestrator checkpointer (DynamoDBSaver) because:
            1. Multi-turn conversations: When sub-agent returns input_required, the follow-up
               message only contains the user's response (e.g., "DM"), not the full context
            2. The context_id is used as thread_id to maintain conversation continuity
            3. State persists across requests since DynamoDBSaver is persistent storage
            4. The orchestrator only sends the LAST message to sub-agents, so sub-agents
               need their own checkpointing to maintain conversation context
            5. If no checkpointer is provided, falls back to stateless (no multi-turn support)

        TODO: shall we handle streaming responses here?
        """
        # Extract content and IDs from input_data
        content = self._extract_message_content(input_data)
        context_id, task_id = self._extract_tracking_ids(input_data)

        try:
            # Ensure agent is created (lazy initialization)
            agent = await self._ensure_agent()

            agent_input = {"messages": [HumanMessage(content=content)]}

            # CRITICAL: Use unique thread_id for checkpoint isolation from orchestrator.
            # The orchestrator and dynamic sub-agents share the same DynamoDB checkpointer instance,
            # so we MUST use different thread_id values to prevent checkpoint pollution, since
            # for some reason using only checkpoint_ns is insufficient to isolate checkpoints.
            #
            # Format: {context_id}::dynamic-{agent_name}
            # - Maintains relationship to parent conversation via context_id prefix
            # - Ensures complete isolation via unique partition key
            # - Enables multi-turn conversations within each sub-agent
            #
            # Note: __pregel_checkpointer is required to prevent LangGraph from interpreting
            # checkpoint_ns as a subgraph identifier (see LangGraph pregel/main.py:1244)
            config: Dict[str, Any] = (
                {
                    "configurable": {
                        "thread_id": f"{context_id}::dynamic-{self.name}",
                        "checkpoint_ns": f"dynamic-{self.name}",
                        "__pregel_checkpointer": self.checkpointer,
                    }
                }
                if context_id and self.checkpointer
                else {}
            )

            # Add sub_agent tag for cost tracking if available
            # Note: Parent tags (user:, conversation:) are automatically propagated by LangGraph
            if self.sub_agent_id:
                config["tags"] = [f"sub_agent:{self.sub_agent_id}"]
                logger.info(f"[COST TRACKING] Sub-agent '{self.name}' added sub_agent:{self.sub_agent_id} tag")
            else:
                logger.warning(
                    f"[COST TRACKING] No sub_agent_id for '{self.name}' - costs won't be tracked by sub-agent"
                )

            # Invoke the agent
            logger.debug(f"Invoking dynamic agent '{self.name}' with content: {content[:100]}...")
            result = await agent.ainvoke(agent_input, config=config)

            # Extract response from agent result
            return self._translate_agent_result(result, context_id, task_id)

        except GraphInterrupt as gi:
            # is not an error - just an interrupt from the graph execution
            logger.info(f"[DYNAMIC AGENT] Graph interrupted in '{self.name}': {gi}")
            # Re-raise so the orchestrator can handle it properly
            raise

        except Exception as e:
            logger.exception(f"Error in dynamic agent '{self.name}': {e}")

            # Check if this is an MCP discovery error
            if "MCP" in str(e) or "tool" in str(e).lower():
                return self._build_error_response(
                    f"Failed to initialize agent tools: {e}",
                    context_id=context_id,
                    task_id=task_id,
                )

            return self._build_error_response(
                f"Agent execution error: {e}",
                context_id=context_id,
                task_id=task_id,
            )

    def _translate_agent_result(
        self,
        result: Dict[str, Any],
        context_id: Optional[str],
        task_id: Optional[str],
    ) -> Dict[str, Any]:
        """Translate LangGraph agent result to A2A protocol format.

        The agent uses SubAgentResponseSchema for structured output, so we
        extract the task_state and message from the structured response.

        This eliminates guessing based on message patterns - the LLM explicitly
        declares its task state just like the orchestrator does.

        Args:
            result: The LangGraph agent's result dict
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID for this invocation

        Returns:
            Dict with 'messages' and A2A metadata
        """
        # Check for structured_response (AutoStrategy for OpenAI)
        structured_response = result.get("structured_response")
        if structured_response and isinstance(structured_response, SubAgentResponseSchema):
            return self._build_response_from_schema(structured_response, context_id, task_id)

        # Check messages for tool call with SubAgentResponseSchema (Bedrock)
        logger.info(f"Translating agent result for '{self.name}'")
        messages = result.get("messages", [])
        for msg in reversed(messages):
            # Check if this is a tool message with SubAgentResponseSchema result
            if hasattr(msg, "name") and msg.name == "SubAgentResponseSchema":
                try:
                    # The tool returns a SubAgentResponseSchema instance
                    if isinstance(msg.content, SubAgentResponseSchema):
                        return self._build_response_from_schema(msg.content, context_id, task_id)
                except Exception as e:
                    logger.warning(f"Failed to parse SubAgentResponseSchema from tool message: {e}")

            # Check for tool_calls that invoked SubAgentResponseSchema
            if hasattr(msg, "tool_calls"):
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "SubAgentResponseSchema":
                        try:
                            schema = SubAgentResponseSchema(**tool_call.get("args", {}))
                            return self._build_response_from_schema(schema, context_id, task_id)
                        except Exception as e:
                            logger.warning(f"Failed to parse SubAgentResponseSchema from tool_call: {e}")

        # Fallback: If no structured response found, extract last message and treat as completed
        # This shouldn't happen if the agent follows the protocol correctly
        if messages:
            last_message = messages[-1]
            content = last_message.content if hasattr(last_message, "content") else str(last_message)
            logger.warning(f"No structured response found for '{self.name}', falling back to completed state")
            return self._build_success_response(content, context_id=context_id, task_id=task_id)

        return self._build_error_response(
            "Agent returned no response",
            context_id=context_id,
            task_id=task_id,
        )

    def _build_response_from_schema(
        self,
        schema: SubAgentResponseSchema,
        context_id: Optional[str],
        task_id: Optional[str],
    ) -> Dict[str, Any]:
        """Build A2A response from SubAgentResponseSchema.

        Args:
            schema: The structured response from the agent
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID for this invocation

        Returns:
            Dict with 'messages' and A2A metadata
        """
        if schema.task_state == "completed":
            return self._build_success_response(schema.message, context_id=context_id, task_id=task_id)
        elif schema.task_state == "input_required":
            return self._build_input_required_response(schema.message, context_id=context_id, task_id=task_id)
        else:  # failed
            return self._build_error_response(schema.message, context_id=context_id, task_id=task_id)


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
    agent_settings: Optional[Any] = None,
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
        agent_settings: AgentSettings for IndexingStoreBackend (enables semantic indexing)

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
        agent_settings=agent_settings,
    )

    return CompiledSubAgent(
        name=config.name,
        description=config.description,
        runnable=runnable,  # type: ignore[arg-type]
    )
