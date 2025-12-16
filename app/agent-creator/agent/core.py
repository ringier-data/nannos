"""Agent Creator - Designs and creates specialized AI agents.

This module implements an A2A agent that helps users create and configure
specialized sub-agents through natural language conversation.
"""

import logging
import os
from collections.abc import AsyncIterable, Awaitable, Callable
from contextvars import ContextVar
from typing import Optional

import boto3
from a2a.types import Task, TaskState
from botocore.config import Config as BotoConfig
from deepagents import create_deep_agent
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langgraph.graph.state import CompiledStateGraph
from langgraph_checkpoint_aws import DynamoDBSaver
from pydantic import BaseModel, Field
from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig
from ringier_a2a_sdk.oauth import OidcOAuth2Client

logger = logging.getLogger(__name__)

# Context variables for thread-safe credential storage
_current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)
_current_access_token: ContextVar[Optional[str]] = ContextVar("current_access_token", default=None)


class UserCredentialInjector:
    """Interceptor that injects user credentials into MCP tool calls.

    This interceptor adds the Authorization Bearer token and X-User-Id header
    to every MCP tool call via the MCPToolCallRequest.headers field.

    The MCP adapter (langchain_mcp_adapters) handles creating new connections
    with these headers when session=None is used during tool loading.

    Uses contextvars for thread-safe credential storage across concurrent requests.
    """

    async def __call__(
        self,
        request: MCPToolCallRequest,
        handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
    ) -> MCPToolCallResult:
        """Inject user credentials into the request headers.

        Args:
            request: The MCP tool call request
            handler: The next handler in the interceptor chain

        Returns:
            The result from the handler
        """
        # Get credentials from context variables (thread-safe)
        user_id = _current_user_id.get()
        access_token = _current_access_token.get()

        if not user_id or not access_token:
            logger.error(
                f"Credentials not set in context: user_id={user_id}, access_token={'SET' if access_token else 'NOT SET'}"
            )
            raise ValueError("Credentials not set in context. This should not happen.")

        tool_name = request.name
        logger.info(f"Injecting credentials for tool '{tool_name}', user={user_id}")

        # Get existing headers or create new dict
        headers = dict(request.headers) if request.headers else {}

        # Inject credentials:
        # - Authorization: The exchanged user token (via OIDC token exchange RFC 8693)
        #   Token exchange preserves the user's sub claim, so backend can look up user directly
        oauth2_client = OidcOAuth2Client(
            client_id=os.getenv("OIDC_CLIENT_ID", "agent-creator"),
            client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
            issuer=os.getenv("OIDC_ISSUER", ""),
        )
        mcp_gateway_token = await oauth2_client.exchange_token(
            subject_token=access_token,
            target_client_id="mcp-gateway",
            requested_scopes=["openid", "profile", "offline_access"],
        )
        headers["Authorization"] = f"Bearer {mcp_gateway_token}"

        logger.info(f"Headers for '{tool_name}': {list(headers.keys())}")

        # Create modified request with updated headers
        # The MCP adapter will use these headers to create a new connection
        modified_request = request.override(headers=headers)

        # Call the handler with modified request
        return await handler(modified_request)


# System prompt for the agent creator
AGENT_CREATOR_SYSTEM_PROMPT = """You are an expert AI Agent Creator for the Alloy Infrastructure Agents platform. Your role is to design, create, and manage specialized sub-agents based on user requirements.

## Your Capabilities

You have access to four nannos tools:
1. **playground_list_sub_agents** - View existing sub-agents to avoid duplicates and understand the current agent ecosystem
2. **playground_create_sub_agent** - Create new sub-agents with specific configurations
3. **playground_update_sub_agent** - Modify existing sub-agents to improve or fix their configurations
4. **playground_list_mcp_tools** - Discover available MCP tools that can be assigned to agents

## Agent Creation Best Practices

### 1. Understanding Requirements
Before creating an agent, thoroughly understand:
- What specific tasks or domain the agent should handle
- What tools or capabilities it needs
- How specialized vs. general-purpose it should be
- What model (GPT-4o, GPT-4o-mini, Claude Sonnet 4.5, or Claude Haiku 4.5) is most appropriate

### 2. Naming Conventions
- Use **lowercase letters, numbers, and hyphens only** (pattern: /^[a-z0-9-]+$/)
- Names should be descriptive and specific (e.g., "jira-ticket-creator", "code-reviewer", "data-analyst")
- Keep names concise but meaningful (2-4 words)
- Avoid generic names like "assistant" or "helper"

### 3. Writing Descriptions
The description is CRITICAL for the orchestrator's routing decisions. Write descriptions that:
- Clearly state the agent's expertise and capabilities
- Use specific keywords related to the domain (e.g., "JIRA", "Python", "data analysis")
- Mention the types of tasks it can handle
- Be concise but comprehensive (1-3 sentences)
- Example: "Specializes in creating and managing JIRA tickets. Can query JIRA projects, create issues with proper formatting, update ticket status, and add comments."

### 4. Crafting System Prompts
Effective system prompts should:
- Start with a clear role definition: "You are a [specific role] that..."
- List specific capabilities and expertise
- Define boundaries - what the agent should NOT do
- Include output format requirements if applicable
- Provide examples of successful task completion
- Be detailed but focused (200-500 words typically)

#### System Prompt Template:
```
You are a [SPECIFIC ROLE] specialized in [DOMAIN/TASKS].

Your expertise includes:
- [Capability 1]
- [Capability 2]
- [Capability 3]

Your primary responsibilities:
1. [Task type 1 with specific details]
2. [Task type 2 with specific details]
3. [Task type 3 with specific details]

Guidelines:
- [Important constraint or guideline 1]
- [Important constraint or guideline 2]
- Always [specific behavior expected]
- Never [specific behavior to avoid]

When completing tasks:
1. [Step or consideration 1]
2. [Step or consideration 2]
3. [Step or consideration 3]

Output format:
[Specify if there are specific formatting requirements]
```

### 5. Selecting the Right Model
- **GPT-4o**: Best for general-purpose tasks, faster responses, strong coding capabilities
- **GPT-4o-mini**: Cost-effective option for simpler tasks, faster responses, good for routine operations
- **Claude Sonnet 4.5**: Best for detailed analysis, longer context understanding, nuanced communication, supports thinking mode
- **Claude Haiku 4.5**: Ultra-fast and cost-efficient for high-volume, low-latency tasks

### 6. Configuring Agent Type
- **Local agents** (type: "local"): Run in-process with custom system prompts and tool access
  - Require: system_prompt, model
  - Optional: mcp_tools (for Gatana gateway tools), system_tools (for platform management)
  - Best for: Custom workflows, specialized tasks, agents needing orchestrator tools

- **Remote agents** (type: "remote"): External A2A-compatible services
  - Require: agent_url (A2A endpoint)
  - Best for: Existing external services, microservice architectures

- **Foundry agents** (type: "foundry"): Palantir Foundry integration
  - Require: foundry_hostname, client credentials, ontology configuration
  - Best for: Foundry data operations and queries

### 7. Tool Selection Strategy
When configuring MCP tools:
- Only select tools the agent actually needs for its tasks
- Fewer tools = clearer focus and better performance
- If unsure, start without specific tools (inherits orchestrator tools)
- Common tool categories:
  - Data access: database queries, file operations
  - Communication: email, messaging, notifications
  - External APIs: JIRA, GitHub, Slack integrations
  - Analysis: data processing, code analysis

### 8. Access Control
- Set `is_public: false` by default (requires group permissions)
- Set `is_public: true` only for genuinely universal agents
- Consider who should have access when designing the agent

## Your Workflow

### When a user asks you to create an agent:

1. **Discovery Phase**
   - Ask clarifying questions about the agent's purpose
   - Use `playground_list_sub_agents` to check if a similar agent exists
   - If similar exists, suggest updating instead of duplicating

2. **Design Phase**
   - Propose the agent configuration:
     - Name
     - Description
     - Agent type (local/remote/foundry)
     - Model selection (for local)
     - System prompt (for local)
     - Tools needed (mcp_tools, system_tools)
   - Get user confirmation or refinement

3. **Creation Phase**
   - Use `playground_create_sub_agent` with the finalized configuration
   - Provide the user with:
     - Confirmation of creation
     - The agent's name and description
     - Link to the agent: {PLAYGROUND_FRONTEND_URL}/sub-agents/{sub_agent_id}
     - How to activate and use it
     - Any limitations or considerations

4. **Iteration Phase**
   - If user wants changes, use `playground_update_sub_agent`
   - Explain what was changed and why
   - Suggest testing approaches

### When a user asks you to update an agent:

1. First use `playground_list_sub_agents` to find the agent
2. Ask what specifically should change
3. Use `playground_update_sub_agent` with only the changed fields
4. Explain the impact of the changes

### When a user asks about existing agents:

1. Use `playground_list_sub_agents` to retrieve current agents
2. Present information in an organized, readable format
3. Highlight key capabilities and specializations
4. Suggest improvements or gaps if relevant

## Important Notes

- **Agent descriptions are routing-critical**: The orchestrator uses descriptions to decide which agent to invoke. Make them specific and keyword-rich.
- **Avoid overlap**: Each agent should have a clear, distinct purpose. Similar agents confuse the orchestrator.
- **Start simple**: Create focused agents. It's easier to expand capabilities than to narrow overly-broad agents.
- **Test iteratively**: Create, test, refine. Use the update tool to improve agents based on real usage.
- **Consider the ecosystem**: Think about how this agent fits with other agents. Should it delegate to others or work independently?

## !IMPORTANT

- **ALWAYS provide a link** to the created agent using the format: {PLAYGROUND_FRONTEND_URL}/subagents/{sub_agent_id}
- **ALWAYS validate** that agent names follow the pattern: /^[a-z0-9-]+$/
- **If you feel you would benefit from tools you don't have access to**, communicate it clearly to the user.

## Communication Style

- Be professional and clear
- Explain your reasoning for design decisions
- Ask for confirmation before creating agents
- Provide actionable next steps after creation
- Teach users about agent design principles
- Suggest improvements proactively

Remember: You're not just creating agents, you're architecting an agent ecosystem. Think about clarity, specialization, and long-term maintainability.
"""


class FinalResponseSchema(BaseModel):
    """Schema for final response from Bedrock models."""

    task_state: str = Field(
        ...,
        description="The final state of the task: 'completed', 'failed', 'input_required', or 'working'",
    )
    message: str = Field(
        ...,
        description="A clear, helpful message to the user about the task outcome",
    )


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


class AgentCreator(BaseAgent):
    """Agent Creator - Helps users design and create specialized AI agents.

    This agent uses Claude Sonnet 4.5 via AWS Bedrock and has access to playground
    backend MCP tools for managing the agent lifecycle.

    Architecture:
    - MCP tools discovered once at initialization (unauthenticated)
    - User credentials injected at runtime for tool execution
    - Shared DynamoDB checkpointer for conversation persistence
    - Single graph instance reused across requests
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self):
        """Initialize the Agent Creator.

        Discovers MCP tools from playground backend and creates the DeepAgent graph.
        """
        super().__init__()

        # Configuration from environment
        self.playground_backend_url = os.getenv("PLAYGROUND_BACKEND_URL", "http://localhost:5001")
        self.playground_frontend_url = os.getenv("PLAYGROUND_FRONTEND_URL", "http://localhost:5173")
        self.bedrock_region = os.getenv("AWS_BEDROCK_REGION", "eu-central-1")
        self.bedrock_model_id = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

        # Checkpointer configuration
        checkpoint_table = os.getenv("CHECKPOINT_DYNAMODB_TABLE_NAME", "agent-creator-checkpoints")
        checkpoint_region = os.getenv("CHECKPOINT_AWS_REGION", "eu-central-1")
        checkpoint_ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
        checkpoint_compression = os.getenv("CHECKPOINT_COMPRESSION_ENABLED", "true").lower() == "true"
        checkpoint_s3_bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")

        # Create shared checkpointer
        s3_config = None
        if checkpoint_s3_bucket:
            s3_config = {"bucket_name": checkpoint_s3_bucket}
            logger.info(f"S3 offloading enabled for large checkpoints: {checkpoint_s3_bucket}")

        self._checkpointer = DynamoDBSaver(
            table_name=checkpoint_table,
            region_name=checkpoint_region,
            ttl_seconds=checkpoint_ttl_days * 24 * 60 * 60,
            enable_checkpoint_compression=checkpoint_compression,
            s3_offload_config=s3_config,  # type: ignore[arg-type]
        )
        logger.info(f"Initialized DynamoDB checkpointer: {checkpoint_table}")

        # Create credential injection interceptor (credentials set per-request via contextvars)
        self._credential_injector = UserCredentialInjector()

        # MCP tools will be discovered lazily on first request
        # (Can't await in __init__, so we defer discovery)
        self._mcp_tools: Optional[list[BaseTool]] = None
        self._mcp_tools_lock = False  # Simple flag to prevent concurrent discovery
        logger.info("MCP tool discovery will happen on first request")

        # Configure boto3 client with timeouts and retry logic from environment variables
        # to handle long-running Claude Sonnet 4.5 requests
        read_timeout = int(os.getenv("BEDROCK_READ_TIMEOUT", "300"))  # Default: 5 minutes
        connect_timeout = int(os.getenv("BEDROCK_CONNECT_TIMEOUT", "10"))  # Default: 10 seconds
        max_attempts = int(os.getenv("BEDROCK_MAX_RETRY_ATTEMPTS", "3"))  # Default: 3 retries
        retry_mode = os.getenv("BEDROCK_RETRY_MODE", "adaptive")  # Default: adaptive

        boto_config = BotoConfig(
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
            retries={
                "max_attempts": max_attempts,
                "mode": retry_mode,
            },
        )

        # Create bedrock-runtime client with custom configuration
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=self.bedrock_region,
            config=boto_config,
        )

        logger.info(
            f"Created Bedrock client with read_timeout={read_timeout}s, "
            f"connect_timeout={connect_timeout}s, max_retry_attempts={max_attempts} ({retry_mode} mode)"
        )

        # Create the model
        self._model = ChatBedrockConverse(
            client=bedrock_client,
            region_name=self.bedrock_region,
            model=self.bedrock_model_id,
            temperature=0,
        )
        logger.info(f"Initialized Bedrock model: {self.bedrock_model_id}")

        self._graph: CompiledStateGraph | None = None
        self._mcp_client: Optional[MultiServerMCPClient] = None

    async def _ensure_mcp_tools_loaded(self):
        """Ensure MCP tools are discovered and loaded.

        This is called lazily on first request to avoid blocking __init__.
        Uses a simple lock to prevent concurrent discovery.
        """
        if self._mcp_tools is not None:
            return  # Already loaded

        if self._mcp_tools_lock:
            # Another request is loading, wait a bit
            import asyncio

            for _ in range(10):  # Wait up to 1 second
                await asyncio.sleep(0.1)
                if self._mcp_tools is not None:
                    return
            logger.warning("Timeout waiting for MCP tools discovery")
            return

        # Acquire lock and discover tools
        self._mcp_tools_lock = True
        try:
            logger.info("Discovering MCP tools from playground backend...")

            # Create MCP client without credentials
            playground_mcp_url = f"{self.playground_backend_url}/mcp"
            connections = {
                "playground": StreamableHttpConnection(
                    transport="streamable_http",
                    url=playground_mcp_url,
                )
            }

            # Create MCP client
            self._mcp_client = MultiServerMCPClient(connections=connections)

            # Load tools with interceptor for credential injection
            # Pass session=None so tools create sessions per-call with modified headers
            from langchain_mcp_adapters.tools import load_mcp_tools

            self._mcp_tools = await load_mcp_tools(
                session=None,  # Important: tools create sessions per-call
                connection=connections["playground"],
                tool_interceptors=[self._credential_injector],
                server_name="playground",
            )

            logger.info(f"Discovered {len(self._mcp_tools)} MCP tools")

            # Recreate graph with MCP tools
            logger.info("Recreating graph with MCP tools...")
            system_prompt = AGENT_CREATOR_SYSTEM_PROMPT.replace(
                "{PLAYGROUND_FRONTEND_URL}", self.playground_frontend_url
            )
            tools = self._mcp_tools + [_create_final_response_tool()]

            self._graph = create_deep_agent(
                model=self._model,
                tools=tools,
                subagents=[],
                system_prompt=system_prompt,
                checkpointer=self._checkpointer,
                middleware=[],  # No middleware needed, interceptor handles it
            )
            logger.info("Graph recreated with MCP tools")
        except Exception as e:
            logger.error(f"Failed to discover MCP tools: {e}", exc_info=True)
            # Use empty list as fallback
            self._mcp_tools = []
            logger.warning("Continuing without MCP tools")
        finally:
            self._mcp_tools_lock = False

    async def _discover_mcp_tools(self) -> list[BaseTool]:
        """Discover MCP tools from playground backend (unauthenticated).

        Note: This discovers tool schemas without authentication.
        Actual tool execution requires user credentials injected at runtime.

        Returns:
            List of MCP tools filtered to the required 4 tools
        """
        connections = {}
        playground_mcp_url = f"{self.playground_backend_url}/mcp"
        connections["playground"] = StreamableHttpConnection(
            transport="streamable_http",
            url=playground_mcp_url,
        )
        client = MultiServerMCPClient(connections=connections)
        tools = await client.get_tools()
        return tools

    async def close(self):
        """Cleanup resources.

        Tools create sessions on-the-fly, so no persistent session to clean up.
        """
        logger.info("AgentCreator closed")

    async def stream(self, query: str, user_config: UserConfig, task: Task) -> AsyncIterable[AgentStreamResponse]:
        """Stream responses for a user query.

        This creates a fresh MCP client with the user's credentials for each request,
        then executes the graph and streams the results.

        Args:
            query: The user's natural language query
            user_config: User configuration including user_id and access_token
            task: The task context for the current interaction

        Yields:
            AgentStreamResponse objects with state updates and content
        """
        try:
            # Ensure MCP tools are loaded before processing
            await self._ensure_mcp_tools_loaded()

            # Validate user credentials
            logger.info(f"Processing query for user {user_config.user_id}")

            if not user_config.access_token:
                raise ValueError("User access token is required for MCP tool execution")

            access_token = user_config.access_token.get_secret_value()

            # Set credentials in context variables (thread-safe for concurrent requests)
            # The interceptor will read these values when tools are called
            _current_user_id.set(user_config.user_id)
            _current_access_token.set(access_token)
            logger.info("Set user credentials in context variables")

            # Execute graph with thread isolation
            config = {
                "configurable": {
                    "thread_id": task.context_id,
                }
            }

            # Convert query to messages format
            from langchain_core.messages import AIMessage, HumanMessage

            input_messages = [HumanMessage(content=query)]

            # Stream graph execution and accumulate final content
            chunk_count = 0
            final_user_content = []  # Accumulate all user-facing content

            async for event in self._graph.astream({"messages": input_messages}, config):
                chunk_count += 1
                logger.debug(f"Graph event #{chunk_count}: {type(event)}")

                # LangGraph returns dict events with node names as keys
                if isinstance(event, dict):
                    # Extract messages from the event
                    for node_name, node_data in event.items():
                        if isinstance(node_data, dict) and "messages" in node_data:
                            messages = node_data["messages"]
                            if isinstance(messages, list):
                                for msg in messages:
                                    if isinstance(msg, AIMessage) and msg.content:
                                        content = str(msg.content)
                                        logger.debug(f"Content from {node_name}: {content[:100]}...")
                                        # Accumulate content for final response
                                        final_user_content.append(content)
                                        # Stream content as working state (progress updates)
                                        yield AgentStreamResponse(
                                            state=TaskState.working,
                                            content=content,
                                        )

            logger.debug(f"Stream processing complete. Total chunks: {chunk_count}")

            # Get final state to check for interrupts and extract final response
            final_state = self._graph.get_state(config)

            # Check for interrupts
            if final_state.interrupts:
                yield AgentStreamResponse(
                    state=TaskState.input_required,
                    content="Process interrupted. Additional input required.",
                )
                return

            # Extract final state from FinalResponseSchema tool call (if present)
            task_state = TaskState.completed

            if final_state.values and "messages" in final_state.values:
                messages = final_state.values["messages"]
                # Look for the last AI message with FinalResponseSchema tool call
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage):
                        # Check if FinalResponseSchema tool was called
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            for tool_call in msg.tool_calls:
                                if tool_call.get("name") == "FinalResponseSchema":
                                    args = tool_call.get("args", {})
                                    state_str = args.get("task_state", "completed")
                                    # Map string state to TaskState enum
                                    if state_str == "input_required":
                                        task_state = TaskState.input_required
                                    elif state_str == "failed":
                                        task_state = TaskState.failed
                                    elif state_str == "working":
                                        task_state = TaskState.working
                                    else:
                                        task_state = TaskState.completed
                                    logger.info(f"FinalResponseSchema found: state={task_state}")
                                    break

                        if task_state != TaskState.completed or msg.tool_calls:
                            break

            # Send final completion with the accumulated user-facing content
            # The FinalResponseSchema's "message" field is for internal tracking, not displayed to user
            final_content = "\n\n".join(final_user_content) if final_user_content else "Request processed successfully."

            logger.info(f"Sending final completion: task_state={task_state}, content_length={len(final_content)}")
            yield AgentStreamResponse(
                state=task_state,
                content=final_content,
            )
            logger.info("Final response sent successfully")

        except Exception as e:
            logger.error(f"Error in AgentCreator.stream: {e}", exc_info=True)
            yield AgentStreamResponse(
                state=TaskState.failed,
                content=f"An error occurred while processing your request: {str(e)}",
                metadata={"error": str(e)},
            )
