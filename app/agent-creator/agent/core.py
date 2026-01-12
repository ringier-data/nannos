"""Agent Creator - Designs and creates specialized AI agents.

This module implements an A2A agent that helps users create and configure
specialized subagents through natural language conversation.
"""

import logging
import os
from collections.abc import Awaitable, Callable

from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langgraph.types import Command
from mcp.types import CallToolResult
from pydantic import BaseModel, Field
from ringier_a2a_sdk.agent import LangGraphBedrockAgent
from ringier_a2a_sdk.cost_tracking.logger import get_request_credentials
from ringier_a2a_sdk.oauth import OidcOAuth2Client

logger = logging.getLogger(__name__)


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
        handler: Callable[[MCPToolCallRequest], Awaitable[CallToolResult | ToolMessage | Command]],
    ) -> CallToolResult | ToolMessage | Command:
        """Inject user credentials into the request headers.

        Args:
            request: The MCP tool call request
            handler: The next handler in the interceptor chain

        Returns:
            The result from the handler
        """
        # Get credentials from context variables (thread-safe)
        user_id, access_token = get_request_credentials()

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
AGENT_CREATOR_SYSTEM_PROMPT = """You are an expert AI Agent Creator for the Alloy Infrastructure Agents platform. Your role is to design, create, and manage specialized subagents based on user requirements.

## Your Capabilities

You have access to four nannos tools:
1. **playground_list_sub_agents** - View existing subagents to avoid duplicates and understand the current agent ecosystem
2. **playground_create_sub_agent** - Create new subagents with specific configurations
3. **playground_update_sub_agent** - Modify existing subagents to improve or fix their configurations
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
     - Link to the agent: {PLAYGROUND_FRONTEND_URL}/app/subagents/{sub_agent_id}
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


class AgentCreator(LangGraphBedrockAgent):
    """Agent Creator - Helps users design and create specialized AI agents.

    This agent uses Claude Sonnet 4.5 via AWS Bedrock and has access to playground
    backend MCP tools for managing the agent lifecycle.

    Architecture:
    - Extends LangGraphBedrockAgent base class
    - MCP tools discovered once at initialization (unauthenticated)
    - User credentials injected at runtime for tool execution via UserCredentialInjector
    - Shared DynamoDB checkpointer for conversation persistence
    """

    def __init__(self):
        """Initialize the Agent Creator."""
        # Store configuration before calling super().__init__()
        self.playground_backend_url = os.getenv("PLAYGROUND_BACKEND_URL", "http://localhost:5001")
        self.playground_frontend_url = os.getenv("PLAYGROUND_FRONTEND_URL", "http://localhost:5173")

        # Create credential injection interceptor
        self._credential_injector = UserCredentialInjector()

        super().__init__()

    async def startup(self):
        """Async startup hook to be called from FastAPI lifespan/startup event."""
        if hasattr(self, "_cost_logger") and self._cost_logger:
            await self._cost_logger.start()
            logger.info("Cost logger background worker started")

    async def shutdown(self):
        """Async shutdown hook to be called from FastAPI lifespan/shutdown event."""
        if hasattr(self, "_cost_logger") and self._cost_logger:
            await self._cost_logger.shutdown()
            logger.info("Cost logger shutdown complete")

    # Abstract method implementations

    def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
        """Return MCP server connection for playground backend.

        Returns connection without authentication - credentials are injected
        at runtime via UserCredentialInjector.
        """
        playground_mcp_url = f"{self.playground_backend_url}/mcp"
        return {
            "playground": StreamableHttpConnection(
                transport="streamable_http",
                url=playground_mcp_url,
            )
        }

    def _get_system_prompt(self) -> str:
        """Return agent creator system prompt with configured frontend URL."""
        return AGENT_CREATOR_SYSTEM_PROMPT.replace("{PLAYGROUND_FRONTEND_URL}", self.playground_frontend_url)

    def _get_checkpoint_namespace(self) -> str:
        """Return checkpoint namespace for agent-creator."""
        return "agent-creator"

    def _get_bedrock_model_id(self) -> str:
        """Return Bedrock model ID for agent creator."""
        return os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

    def _get_tool_interceptors(self) -> list:
        """Return credential injector for MCP tool calls."""
        return [self._credential_injector]
