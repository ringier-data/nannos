"""Dynamic Local Agent - User-specific sub-agents with LangGraph execution.

This module provides a mechanism for dynamically provisioning user-specific
sub-agents that run in-process but communicate via the A2A protocol.

Unlike the file-analyzer (simple local agent) or remote A2A agents (network calls),
DynamicLocalAgentRunnable wraps a full LangGraph agent with:
- Custom system prompts (user-configurable)
- Optional MCP tool discovery (lazy-loaded on first invocation)
- Standard A2A state responses (completed, input_required, failed)
- Structured output for explicit task state determination (no guessing)

Architecture:
- Inherits from LocalA2ARunnable for A2A protocol compliance
- Lazily creates a LangGraph agent on first invocation
- If mcp_gateway_url is set, discovers tools from that gateway (overriding orchestrator tools)
- If mcp_gateway_url is None, inherits tools from orchestrator's tool_registry
- Uses structured output (SubAgentResponseSchema) for explicit task state

Use Case:
Users can configure personal sub-agents via DynamoDB with custom prompts and optional
dedicated MCP servers, enabling specialized assistants without deploying separate A2A services.
"""

import logging
from typing import Any, Dict, List, Literal, Optional

from deepagents import CompiledSubAgent
from langchain.agents import create_agent
from langchain.agents.structured_output import AutoStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, Field
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from .base import LocalA2ARunnable
from .models import LocalSubAgentConfig

logger = logging.getLogger(__name__)


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


def _create_subagent_response_tool() -> BaseTool:
    """Create the SubAgentResponseSchema tool for Bedrock models.

    Bedrock models don't support response_format, so we use a tool instead.

    Returns:
        StructuredTool for sub-agent response handling
    """

    def response_handler(**kwargs):
        """Handler for SubAgentResponseSchema tool - returns the structured response."""
        return SubAgentResponseSchema(**kwargs)

    return StructuredTool.from_function(
        func=response_handler,
        name="SubAgentResponseSchema",
        description=(
            "REQUIRED: You MUST call this tool to provide your response. "
            "This tool signals your task state (completed, input_required, or failed) "
            "and provides your message to the orchestrator."
        ),
        args_schema=SubAgentResponseSchema,
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
    - Optional MCP gateway for tool discovery (lazy-loaded)
    - Inheritance of orchestrator tools when no MCP gateway is specified
    - Standard A2A protocol responses (completed, input_required, failed)

    The agent is created lazily to:
    1. Defer MCP tool discovery until the agent is actually used
    2. Allow tool inheritance from orchestrator context at invocation time
    3. Avoid resource consumption for agents that may never be called

    Attributes:
        config: LocalSubAgentConfig with name, description, system_prompt, mcp_gateway_url
        model: The LangGraph model to use for the agent
        orchestrator_tools: Tools inherited from orchestrator (used if mcp_gateway_url is None)
        _agent: Lazily-created LangGraph agent (cached after first creation)
        _discovered_tools: Tools discovered from MCP gateway (cached after discovery)
    """

    def __init__(
        self,
        config: LocalSubAgentConfig,
        model: BaseChatModel,
        orchestrator_tools: Optional[List[BaseTool]] = None,
        oauth2_client: Optional[OidcOAuth2Client] = None,
        user_token: Optional[str] = None,
        checkpointer: Optional[BaseCheckpointSaver] = None,
    ):
        """Initialize the dynamic local agent runnable.

        Args:
            config: Configuration with name, description, system_prompt, mcp_gateway_url
            model: The LangGraph model to use for the agent
            orchestrator_tools: Tools inherited from orchestrator (used if config.mcp_gateway_url is None)
            oauth2_client: OAuth2 client for token exchange (required if mcp_gateway_url is set)
            user_token: User's access token for token exchange (required if mcp_gateway_url is set)
            checkpointer: Shared checkpointer for multi-turn conversation state (e.g., DynamoDBSaver)
        """
        self.config = config
        self.model = model
        self.orchestrator_tools = orchestrator_tools or []
        self.oauth2_client = oauth2_client
        self.user_token = user_token
        self.checkpointer = checkpointer
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

    async def _discover_mcp_tools(self) -> List[BaseTool]:
        """Discover tools from the configured MCP gateway with authentication.

        This is called lazily on first invocation if mcp_gateway_url is set.
        Uses the same token exchange flow as the orchestrator's ToolDiscoveryService
        to authenticate with the MCP gateway.

        The discovered tools override orchestrator tools entirely.

        Returns:
            List of discovered BaseTool instances

        Raises:
            Exception: If MCP discovery fails (will result in failed state)
        """
        if not self.config.mcp_gateway_url:
            return []

        logger.info(f"Discovering MCP tools for {self.name} from {self.config.mcp_gateway_url}")

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
                    "dynamic": StreamableHttpConnection(
                        transport="streamable_http",
                        url=self.config.mcp_gateway_url,
                        headers=headers if headers else None,
                    )
                }
            )

            tools = await client.get_tools()
            logger.info(f"Discovered {len(tools)} MCP tools for {self.name}")
            return tools

        except Exception as e:
            logger.error(f"Failed to discover MCP tools for {self.name}: {e}")
            raise

    def _get_effective_tools(self) -> List[BaseTool]:
        """Get the effective tools for this agent.

        If MCP tools were discovered, use those (override).
        Otherwise, use orchestrator tools (inheritance).

        Returns:
            List of tools to use for the agent
        """
        if self._discovered_tools is not None:
            return self._discovered_tools
        return self.orchestrator_tools

    async def _ensure_agent(self) -> Any:
        """Ensure the LangGraph agent is created (lazy initialization).

        On first call:
        1. If mcp_gateway_url is set, discover tools from that gateway
        2. Otherwise, use orchestrator tools
        3. Create the LangGraph agent with structured output for task state

        The agent uses SubAgentResponseSchema to explicitly determine task state,
        following the same pattern as the orchestrator's FinalResponseSchema.

        For Bedrock models, we add SubAgentResponseSchema as a tool.
        For OpenAI models, we use AutoStrategy for structured output.

        Returns:
            The compiled LangGraph agent
        """
        if self._agent is not None:
            return self._agent

        # Discover MCP tools if configured (lazy, first invocation only)
        if self.config.mcp_gateway_url and self._discovered_tools is None:
            self._discovered_tools = await self._discover_mcp_tools()

        tools = self._get_effective_tools()
        logger.info(f"Creating LangGraph agent '{self.name}' with {len(tools)} tools")

        # Build system prompt with A2A protocol addendum
        system_prompt = self.config.system_prompt + A2A_PROTOCOL_ADDENDUM

        # Check if this is a Bedrock model (needs tool-based structured output)
        is_bedrock = hasattr(self.model, "_client") and "bedrock" in str(type(self.model)).lower()

        if is_bedrock:
            # Bedrock: Add response schema as a tool
            agent_tools = list(tools) + [_create_subagent_response_tool()]
            self._agent = create_agent(
                self.model,
                system_prompt=system_prompt,
                tools=agent_tools,
                checkpointer=self.checkpointer,  # Shared checkpointer for multi-turn conversations
            )
        else:
            # OpenAI: Use structured output via response_format
            self._agent = create_agent(
                self.model,
                system_prompt=system_prompt,
                tools=tools,
                checkpointer=self.checkpointer,  # Shared checkpointer for multi-turn conversations
                response_format=AutoStrategy(schema=SubAgentResponseSchema),
            )

        return self._agent

    async def _process(
        self,
        content: str,
        context_id: Optional[str],
    ) -> Dict[str, Any]:
        """Process the input using the LangGraph agent.

        Creates the agent lazily on first call, then invokes it with the content.
        Translates the agent's response into A2A protocol format.

        Args:
            content: The message content to process
            context_id: Optional context ID for conversation continuity

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
        try:
            # Ensure agent is created (lazy initialization)
            agent = await self._ensure_agent()

            agent_input = {"messages": [HumanMessage(content=content)]}

            # Use context_id as thread_id for conversation continuity when checkpointer is available
            config = {"configurable": {"thread_id": context_id}} if context_id and self.checkpointer else {}

            # Invoke the agent
            logger.debug(f"Invoking dynamic agent '{self.name}' with content: {content[:100]}...")
            result = await agent.ainvoke(agent_input, config=config)

            # Extract response from agent result
            return self._translate_agent_result(result, context_id)

        except Exception as e:
            logger.exception(f"Error in dynamic agent '{self.name}': {e}")

            # Check if this is an MCP discovery error
            if "MCP" in str(e) or "tool" in str(e).lower():
                return self._build_error_response(
                    f"Failed to initialize agent tools: {e}",
                    context_id=context_id,
                )

            return self._build_error_response(
                f"Agent execution error: {e}",
                context_id=context_id,
            )

    def _translate_agent_result(
        self,
        result: Dict[str, Any],
        context_id: Optional[str],
    ) -> Dict[str, Any]:
        """Translate LangGraph agent result to A2A protocol format.

        The agent uses SubAgentResponseSchema for structured output, so we
        extract the task_state and message from the structured response.

        This eliminates guessing based on message patterns - the LLM explicitly
        declares its task state just like the orchestrator does.

        Args:
            result: The LangGraph agent's result dict
            context_id: Optional context ID for conversation continuity

        Returns:
            Dict with 'messages' and A2A metadata
        """
        # Check for structured_response (AutoStrategy for OpenAI)
        structured_response = result.get("structured_response")
        if structured_response and isinstance(structured_response, SubAgentResponseSchema):
            return self._build_response_from_schema(structured_response, context_id)

        # Check messages for tool call with SubAgentResponseSchema (Bedrock)
        messages = result.get("messages", [])
        for msg in reversed(messages):
            # Check if this is a tool message with SubAgentResponseSchema result
            if hasattr(msg, "name") and msg.name == "SubAgentResponseSchema":
                try:
                    # The tool returns a SubAgentResponseSchema instance
                    if isinstance(msg.content, SubAgentResponseSchema):
                        return self._build_response_from_schema(msg.content, context_id)
                except Exception as e:
                    logger.warning(f"Failed to parse SubAgentResponseSchema from tool message: {e}")

            # Check for tool_calls that invoked SubAgentResponseSchema
            if hasattr(msg, "tool_calls"):
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "SubAgentResponseSchema":
                        try:
                            schema = SubAgentResponseSchema(**tool_call.get("args", {}))
                            return self._build_response_from_schema(schema, context_id)
                        except Exception as e:
                            logger.warning(f"Failed to parse SubAgentResponseSchema from tool_call: {e}")

        # Fallback: If no structured response found, extract last message and treat as completed
        # This shouldn't happen if the agent follows the protocol correctly
        if messages:
            last_message = messages[-1]
            content = last_message.content if hasattr(last_message, "content") else str(last_message)
            logger.warning(f"No structured response found for '{self.name}', falling back to completed state")
            return self._build_success_response(content, context_id=context_id)

        return self._build_error_response(
            "Agent returned no response",
            context_id=context_id,
        )

    def _build_response_from_schema(
        self,
        schema: SubAgentResponseSchema,
        context_id: Optional[str],
    ) -> Dict[str, Any]:
        """Build A2A response from SubAgentResponseSchema.

        Args:
            schema: The structured response from the agent
            context_id: Optional context ID for conversation continuity

        Returns:
            Dict with 'messages' and A2A metadata
        """
        if schema.task_state == "completed":
            return self._build_success_response(schema.message, context_id=context_id)
        elif schema.task_state == "input_required":
            return self._build_input_required_response(schema.message, context_id=context_id)
        else:  # failed
            return self._build_error_response(schema.message, context_id=context_id)


def create_dynamic_local_subagent(
    config: LocalSubAgentConfig,
    model: BaseChatModel,
    orchestrator_tools: Optional[List[BaseTool]] = None,
    oauth2_client: Optional[OidcOAuth2Client] = None,
    user_token: Optional[str] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
) -> CompiledSubAgent:
    """Create a dynamic local sub-agent from configuration.

    Factory function that creates a CompiledSubAgent wrapping a DynamicLocalAgentRunnable.
    This can be registered in the orchestrator's subagent_registry for use with the task tool.

    Args:
        config: LocalSubAgentConfig with name, description, system_prompt, mcp_gateway_url
        model: The LangGraph model to use for the agent
        orchestrator_tools: Tools inherited from orchestrator (used if config.mcp_gateway_url is None)
        oauth2_client: OAuth2 client for token exchange (required if mcp_gateway_url is set)
        user_token: User's access token for token exchange (required if mcp_gateway_url is set)
        checkpointer: Shared checkpointer for multi-turn conversation state (e.g., DynamoDBSaver)

    Returns:
        CompiledSubAgent that can be registered with the orchestrator

    Example:
        config = LocalSubAgentConfig(
            name="data-analyst",
            description="Analyzes data and generates insights",
            system_prompt="You are a data analysis expert...",
            mcp_gateway_url=None,  # Inherit orchestrator tools
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
    )

    return CompiledSubAgent(
        name=config.name,
        description=config.description,
        runnable=runnable,  # type: ignore[arg-type]
    )
