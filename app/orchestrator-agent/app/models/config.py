"""Configuration models and settings for the Orchestrator Deep Agent.

This module contains all configuration-related models and settings,
separated from the core agent logic for better maintainability.
"""

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

# Model type literal for type safety
ModelType = Literal["gpt4o", "claude-sonnet-4.5"]


@dataclass
class GraphRuntimeContext:
    """Runtime context injected into LangGraph at invocation time.

    This dataclass is passed to the graph via the `context` parameter, enabling:
    1. Dynamic prompt customization (language, name)
    2. Runtime tool injection (tool_registry)
    3. Runtime subagent injection (subagent_registry)

    DynamicToolDispatchMiddleware uses tool_registry and subagent_registry to:
    - Bind tools to the model at call time (wrap_model_call)
    - Execute tool calls without ToolNode registration (wrap_tool_call)

    This enables a SINGLE graph to serve ALL users with different tool configurations.

    Note: Created from UserConfig via UserConfig.to_runtime_context()
    """

    user_id: str
    """User identifier for logging and isolation."""

    name: str
    """User's display name for personalization."""

    email: str
    """User's email address."""

    language: str = "en"
    """User's preferred language for responses (ISO 639-1 code)."""

    tool_registry: dict[str, Any] = field(default_factory=dict)
    """Registry of tool name -> BaseTool instance for this user.

    Tools are discovered per-user (e.g., from MCP servers) and stored here.
    DynamicToolDispatchMiddleware uses this to:
    1. Bind tools to the model dynamically
    2. Execute tool calls without ToolNode registration
    """

    subagent_registry: dict[str, Any] = field(default_factory=dict)
    """Registry of subagent name -> callable for this user.

    Subagents are discovered per-user and stored here for dynamic invocation.
    """

    @property
    def tools(self) -> list["BaseTool"]:
        """Get list of all tools available to this user."""
        return list(self.tool_registry.values())

    @property
    def tool_names(self) -> list[str]:
        """Get names of all tools available to this user."""
        return list(self.tool_registry.keys())


class ResponseFormat(BaseModel):
    """Format specification for agent responses."""

    type: str = Field(..., description="Response format type (e.g., 'text', 'json')")

    model_config = ConfigDict(arbitrary_types_allowed=True)


class UserConfig(BaseModel):
    """User-specific configuration for personalized agent behavior.

    Contains user credentials, preferences, and discovered tools/sub-agents.
    Use to_runtime_context() to convert to GraphRuntimeContext for graph invocation.
    """

    user_id: str = Field(..., description="User identifier")
    access_token: SecretStr = Field(..., description="User authentication token")
    name: str = Field(..., description="User's full name")
    email: str = Field(..., description="User's email address")
    language: str = Field(default="en", description="User's preferred language")
    model: Optional[ModelType] = Field(default=None, description="LLM model to use (gpt4o or claude-sonnet-4.5)")
    sub_agents: Optional[list] = Field(default=None, description="Discovered sub-agents")
    tools: Optional[list] = Field(default=None, description="Discovered tools")

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def to_runtime_context(self) -> "GraphRuntimeContext":
        """Convert to GraphRuntimeContext for LangGraph execution.

        Transforms discovered tools and subagents lists into registries
        for dynamic tool dispatch at runtime.

        Returns:
            GraphRuntimeContext for graph invocation
        """
        # Convert tools list to tool_registry (name -> tool mapping)
        tool_registry: dict[str, Any] = {}
        for tool in self.tools or []:
            if hasattr(tool, "name"):
                tool_registry[tool.name] = tool
            elif isinstance(tool, dict):
                tool_registry[tool.get("name", str(tool))] = tool

        # Convert subagents list to subagent_registry (name -> CompiledSubAgent mapping)
        subagent_registry: dict[str, Any] = {}
        for subagent in self.sub_agents or []:
            if isinstance(subagent, dict) and "name" in subagent:
                subagent_registry[subagent["name"]] = subagent

        return GraphRuntimeContext(
            user_id=self.user_id,
            name=self.name,
            email=self.email,
            language=self.language,
            tool_registry=tool_registry,
            subagent_registry=subagent_registry,
        )

    model_config = ConfigDict(arbitrary_types_allowed=True)


class DynamoDBConfig(BaseModel):
    """DynamoDB configuration."""

    region: str = Field(default_factory=lambda: os.getenv("AWS_REGION", "eu-central-1"))
    users_table: str = Field(
        default_factory=lambda: os.getenv("DYNAMODB_USERS_TABLE", "dev-alloy-infrastructure-agents-users")
    )


class AgentSettings:
    """Static settings for the Orchestrator Deep Agent.

    Centralizes all environment-based configuration and constants.
    """

    # Retry configuration
    MAX_RETRIES = 3
    BACKOFF_FACTOR = 2.0

    # Cache configuration
    AGENT_DISCOVERY_CACHE_TTL = 30  # seconds

    # DynamoDB checkpoint configuration
    CHECKPOINT_TABLE_NAME = "dev-alloy-infrastructure-agents-langgraph-checkpoints"
    CHECKPOINT_TTL_DAYS = 14
    CHECKPOINT_AWS_REGION = "eu-central-1"
    CHECKPOINT_MAX_RETRIES = 5

    # System prompt
    SYSTEM_INSTRUCTION = (
        "You are an orchestrator agent that plans how to best fulfill user requests by "
        "analyzing the query and context, then planning and coordinating the execution of tasks via sub-agents. "
        "You can use the todo list tool to keep a todo list of tasks to accomplish the user's goals, and delegate these tasks to appropriate sub-agents. "
        "You must decide which sub-agents to invoke, in what order, and how to handle their outputs. "
        "\n\n"
        "**IMPORTANT - Todo List Management:**\n"
        '- ALWAYS update the todo list when starting work on a task (set status to "in_progress")\n'
        '- ALWAYS update the todo list when completing a task (set status to "completed")\n'
        "- ALWAYS update the todo list before completing the user request to reflect any remaining tasks\n"
        "- Keep the user informed of progress by consistently updating task statuses\n"
        "- The todo list is your primary way to communicate progress to the user\n"
        "\n"
        "**CRITICAL - Final Task Status Determination:**\n"
        "When you provide your final response, you MUST explicitly determine the appropriate task_state based on:\n"
        '1. **Todo List State**: Check if all todos are "completed", or if some are "pending"/"in_progress"\n'
        "2. **User Request Satisfaction**: Has the user's original request been fully accomplished?\n"
        "3. **Need for Additional Input**: Is there missing information that blocks further progress?\n"
        "\n"
        "Choose the task_state carefully:\n"
        "- **completed**: All todos done AND user request fully satisfied AND no further action needed\n"
        "- **working**: Long-running task with pending/in-progress todos that will continue asynchronously\n"
        "- **input_required**: Blocked on user input/clarification AND cannot proceed without it\n"
        "- **failed**: Encountered unrecoverable error OR cannot complete the task\n"
        "\n"
        "Your final response MUST include both the task_state and a clear message to the user.\n"
        "\n"
        "Each sub-agent follows the A2A protocol for task execution and reporting. When a sub-agent returns:\n"
        '- "input_required": The sub-agent needs information from the USER. Ask the user for the required input.\n'
        '- "auth_required": The sub-agent needs authentication. Ask the user to complete the authentication flow.\n'
        '- "completed": The sub-agent finished successfully. Use the results to respond to the user.\n'
        '- "failed": The sub-agent encountered an error. Explain the error to the user.\n'
        "\n"
        "Automatic Retry Behavior: Transient failures (network errors, timeouts, temporary service issues) are "
        "automatically retried up to 3 times with exponential backoff. You do NOT need to manually retry these. "
        'Only "input_required" and "auth_required" states require user interaction - retry these just if you realize '
        "you could have provided different information, you have available in the conversation history, to the sub-agent. "
        "Otherwise, communicate clearly to the user and wait for the user to provide the necessary input or complete authentication.\n"
        "\n"
        "ALWAYS delegate tasks to sub-agents rather than trying to do everything yourself.\n"
        "\n"
        "**CRITICAL CONSTRAINT - Tool-Based Planning Only:**\n"
        "YOU ARE NOT THE SMARTEST IN THE ROOM. You MUST NOT attempt to solve tasks directly using your own knowledge or reasoning. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "Before creating any task plan:\n"
        "1. Review your available tools and sub-agents carefully\n"
        "2. Verify each planned task can be accomplished with these capabilities\n"
        "3. If a task cannot be solved with available tools, you MUST adapt your approach:\n"
        "   - Break the task into smaller parts that ARE solvable with available tools\n"
        "   - Find alternative approaches using different available tools\n"
        "   - Inform the user about limitations and propose feasible alternatives\n"
        "4. NEVER plan tasks that require capabilities you do not have\n"
        "5. When in doubt, explicitly check what tools are available before planning\n"
        "\n"
        "If the user sends you an error message about a system issue, plan how to resolve it using your sub-agents. "
        "For example:\n"
        "1. Use general-purpose-agent to understand the error\n"
        "2. Check if any relevant context is missing and request it\n"
        "3. Use ticket-creation-tool to fetch the ticket details\n"
        "4. Use github tools to create an issue for the bug\n"
        "5. Assign appropriate coding agents to fix the bug\n"
        "6. Poll the ticket to find the PR link\n"
        "\n"
        "Remember: Every task in your plan must be achievable with your current toolset."
    )

    @classmethod
    def get_azure_deployment(cls) -> str:
        """Get Azure OpenAI deployment name."""
        return os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]

    @classmethod
    def get_azure_model_name(cls) -> str:
        """Get Azure OpenAI model name."""
        return os.environ["AZURE_OPENAI_CHAT_MODEL_NAME"]

    @classmethod
    def get_oidc_client_id(cls) -> str:
        """Get Okta/Keycloak OAuth2 client ID."""
        return os.environ["OIDC_CLIENT_ID"]

    @classmethod
    def get_oidc_client_secret(cls) -> SecretStr:
        """Get Okta/Keycloak OAuth2 client secret."""
        return SecretStr(os.environ["OIDC_CLIENT_SECRET"])

    @classmethod
    def get_oidc_issuer(cls) -> str:
        """Get Okta/Keycloak issuer URL."""
        return os.environ["OIDC_ISSUER"]

    @classmethod
    def get_bedrock_model_id(cls) -> str:
        """Get AWS Bedrock model ID."""
        return os.environ["BEDROCK_MODEL_ID"]

    @classmethod
    def get_bedrock_region(cls) -> str:
        """Get AWS Bedrock region."""
        return os.environ.get("AWS_REGION", "eu-central-1")

    # Budget guard configuration
    @classmethod
    def get_budget_enabled(cls) -> bool:
        """Check if budget enforcement is enabled."""
        return os.environ.get("BUDGET_ENABLED", "true").lower() == "true"

    @classmethod
    def get_budget_monthly_token_limit(cls) -> int:
        """Get monthly token limit for budget enforcement.

        Default: 100 million tokens (~$300 for Claude on Bedrock)
        """
        return int(os.environ.get("BUDGET_MONTHLY_TOKEN_LIMIT", "100000000"))

    @classmethod
    def get_budget_check_interval(cls) -> int:
        """Get budget check interval in seconds.

        Default: 300 seconds (5 minutes)
        """
        return int(os.environ.get("BUDGET_CHECK_INTERVAL_SECONDS", "300"))

    @classmethod
    def get_budget_warning_thresholds(cls) -> tuple[float, ...]:
        """Get warning thresholds as percentages (0.0-1.0).

        Default: 80%, 90%, 95%
        """
        thresholds_str = os.environ.get("BUDGET_WARNING_THRESHOLDS", "0.80,0.90,0.95")
        return tuple(float(t.strip()) for t in thresholds_str.split(","))

    @classmethod
    def get_langsmith_project(cls) -> str:
        """Get LangSmith project name for budget tracking."""
        return os.environ.get("LANGSMITH_PROJECT", "dev-alloy-agent-framework")
