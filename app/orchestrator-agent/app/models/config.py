"""Configuration models and settings for the Orchestrator Deep Agent.

This module contains all configuration-related models and settings,
separated from the core agent logic for better maintainability.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional

from deepagents import CompiledSubAgent
from langchain_core.messages import ContentBlock
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..a2a_utils.models import LocalSubAgentConfig
from ..models.base import ModelType, ThinkingLevel

logger = logging.getLogger(__name__)

# Message formatting literal for type safety

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


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

    Note: Created via build_runtime_context(user_config, runtime_deps)
    """

    user_id: str
    """User's database ID (users.id - stable identifier for DB operations and isolation)."""

    user_sub: str
    """User's OIDC sub claim (subject identifier from identity provider - can change with IDP)."""

    name: str
    """User's display name for personalization."""

    email: str
    """User's email address."""

    language: str = "en"
    """User's preferred language for responses (ISO 639-1 code)."""

    timezone: str = "Europe/Zurich"
    """User's preferred timezone (IANA timezone name like 'America/New_York', 'Europe/Berlin')."""

    message_formatting: str = "markdown"
    """Message formatting style for responses.
    
    Values:
    - 'markdown': Standard markdown formatting (default)
    - 'slack': Slack mrkdwn formatting (*bold*, _italic_, `code`, <@U123> mentions)
    - 'plain': Plain text with no formatting
    """

    slack_user_handle: Optional[str] = None
    """Slack user handle for the current speaker (e.g., '<@U123456>').
    
    Used for @-mention generation in Slack conversations. Only set when
    the client is a Slack app. The LLM can use this to tag the current user
    in responses when needed (e.g., for input_required states).
    """

    custom_prompt: Optional[str] = None
    """User's custom prompt addendum.
    
    If set, this text is appended to the system prompt as additional instructions.
    Allows users to customize agent behavior with personal preferences or guidelines.
    """

    tool_registry: dict[str, Any] = field(default_factory=dict)
    """Registry of tool name -> BaseTool instance for this user.

    Tools are discovered per-user (e.g., from MCP servers) and stored here.
    DynamicToolDispatchMiddleware uses this to:
    1. Bind tools to the model dynamically
    2. Execute tool calls without ToolNode registration
    """

    subagent_registry: dict[str, CompiledSubAgent] = field(default_factory=dict)
    """Registry of subagent name -> CompiledSubAgent for this user.

    CompiledSubAgent is a TypedDict with:
    - name: str - The subagent's unique identifier
    - description: str - What the subagent does (shown to LLM)
    - runnable: Runnable - The executable that handles task invocations

    Both remote A2A agents and local sub-agents (file-analyzer, dynamic local agents)
    are stored in this unified registry for dispatch by DynamicToolDispatchMiddleware.
    """

    whitelisted_tool_names: set[str] = field(default_factory=set)
    """Set of tool names that are whitelisted for orchestrator use.
    
    The orchestrator only has access to these tools, while the general-purpose
    agent has access to all tools in tool_registry. This enables different
    tool scopes for orchestrator vs GP agent.
    """

    pending_file_blocks: list[ContentBlock] = field(default_factory=list)
    """File content blocks extracted from the current A2A message's FileParts.

    Ephemeral per-request data (never checkpointed). When the user attaches files,
    their URIs and MIME types are captured here as typed LangChain ContentBlocks
    (ImageContentBlock, FileContentBlock, etc.) and injected deterministically
    into every sub-agent dispatch via HumanMessage.content_blocks.

    This avoids the orchestrator LLM seeing (and hallucinating) raw pre-signed URLs.
    Sub-agents receive the exact original URIs without any LLM round-trip.
    """

    _cached_selected_tools: Optional[list[Any]] = field(default=None, repr=False)
    """Cached tool selection results (internal).
    
    Set by ToolsetSelectorMiddleware after Phase 1 (server selection) and
    Phase 2 (tool selection) to avoid re-running the LLM selections on every
    model call within the same GP invocation. Reset to None between GP
    invocations by GPAgentRunnable._process().
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
    Use build_runtime_context(user_config, runtime_deps) to create GraphRuntimeContext.
    """

    user_id: str = Field(
        description="User's database ID (users.id - stable identifier for DB operations and isolation)",
    )
    user_sub: str = Field(..., description="User's OIDC sub claim (can change with IDP)")
    access_token: SecretStr = Field(..., description="User authentication token")
    name: str = Field(..., description="User's full name")
    email: str = Field(..., description="User's email address")
    groups: list[str] = Field(default_factory=list, description="User's group memberships (from Keycloak groups claim)")
    language: str = Field(default="en", description="User's preferred language")
    timezone: str = Field(default="Europe/Zurich", description="User's preferred timezone (IANA timezone name)")
    model: Optional[ModelType] = Field(
        default=None, description="LLM model to use (gpt4o, gpt-4o-mini, claude-sonnet-4.5, or claude-haiku-4-5)"
    )
    message_formatting: Literal["markdown", "slack", "plain"] = Field(
        default="markdown",
        description="Message formatting style: 'markdown' (default), 'slack', or 'plain'",
    )
    slack_user_handle: Optional[str] = Field(
        default=None,
        description="Slack user handle for @-mentions (e.g., '<@U123456>')",
    )
    custom_prompt: Optional[str] = Field(
        default=None,
        description="User's custom prompt addendum to append to system prompt",
    )
    sub_agent_config_hash: Optional[str] = Field(
        default=None,
        description="Sub-agent config hash for playground testing mode (single sub-agent isolation)",
    )
    agent_metadata: Optional[dict[str, dict[str, Any]]] = Field(
        default=None,
        description="Agent metadata from registry: Maps agent_url -> {sub_agent_id, name, description}",
    )
    tool_names: Optional[list[str]] = Field(
        default=None,
        description="MCP tool names enabled for orchestrator (from registry)",
    )
    sub_agents: Optional[list[CompiledSubAgent]] = Field(
        default=None,
        description="Discovered remote A2A sub-agents (CompiledSubAgent TypedDicts with name, description, runnable)",
    )
    tools: Optional[list] = Field(default=None, description="Discovered MCP tools")
    local_subagents: Optional[list[LocalSubAgentConfig]] = Field(
        default=None,
        description="User-configured local sub-agents",
    )
    enable_thinking: Optional[bool] = Field(
        default=None,
        description="Enable extended thinking for orchestrator (overrides environment variable)",
    )
    thinking_level: Optional[ThinkingLevel] = Field(
        default=None,
        description="Thinking depth level: minimal/low/medium/high (only for Claude Sonnet and Gemini models)",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


class DynamoDBConfig(BaseModel):
    """DynamoDB configuration."""

    region: str = Field(default_factory=lambda: os.getenv("AWS_REGION", "eu-central-1"))
    users_table: str = Field(
        default_factory=lambda: os.getenv("DYNAMODB_USERS_TABLE", "dev-nannos-infrastructure-agents-users")
    )


class AgentSettings:
    """Static settings for the Orchestrator Deep Agent.

    Centralizes all environment-based configuration and constants.
    """

    # Retry configuration
    MAX_RETRIES = 3
    BACKOFF_FACTOR = 3.0

    # Recursion limit configuration (overrides deepagents default of 1000)
    MAX_RECURSION_LIMIT = int(os.getenv("MAX_RECURSION_LIMIT", "50"))

    # Toolset selection configuration (used by ToolsetSelectorMiddleware in custom GP graph)
    TOOLSET_SELECTION_THRESHOLD = int(os.getenv("TOOLSET_SELECTION_THRESHOLD", "50"))
    """Trigger server-level selection (Phase 1) in ToolsetSelectorMiddleware when total MCP tool count exceeds this."""

    TOOL_SELECTION_THRESHOLD = int(os.getenv("TOOL_SELECTION_THRESHOLD", "20"))
    """Trigger tool-level LLM selection (Phase 2) in ToolsetSelectorMiddleware when remaining tools exceed this."""

    TOOLSET_SELECTION_MODEL: ModelType = os.getenv("TOOLSET_SELECTION_MODEL", "gpt-4o-mini")  # type: ignore
    """Model to use for server and tool selection (fast, cheap model preferred)."""

    # Cache configuration
    AGENT_DISCOVERY_CACHE_TTL = 30  # seconds

    # DynamoDB checkpoint configuration
    CHECKPOINT_DYNAMODB_TABLE_NAME = os.getenv(
        "CHECKPOINT_DYNAMODB_TABLE_NAME", "dev-nannos-infrastructure-agents-langgraph-checkpoints"
    )
    CHECKPOINT_TTL_DAYS = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
    CHECKPOINT_AWS_REGION = os.getenv("CHECKPOINT_AWS_REGION", "eu-central-1")
    CHECKPOINT_MAX_RETRIES = int(os.getenv("CHECKPOINT_MAX_RETRIES", "5"))

    # S3 offloading for large checkpoints (>350KB) - prevents DynamoDB 400KB limit errors
    CHECKPOINT_S3_BUCKET_NAME: str | None = os.getenv("CHECKPOINT_S3_BUCKET_NAME", None)
    CHECKPOINT_COMPRESSION_ENABLED = os.getenv("CHECKPOINT_COMPRESSION_ENABLED", "true").lower() == "true"

    # file store configuration
    DOCUMENT_STORE_S3_BUCKET = os.getenv("DOCUMENT_STORE_S3_BUCKET", "dev-nannos-infrastructure-agents-files")

    # MCP gateway configuration
    MCP_GATEWAY_URL = os.getenv("MCP_GATEWAY_URL", "https://alloych.gatana.ai/mcp")
    MCP_GATEWAY_CLIENT_ID = os.getenv("MCP_GATEWAY_CLIENT_ID", "gatana")

    POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
    POSTGRES_DB = os.getenv("POSTGRES_DB", "playground")
    POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
    POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "playground")

    # System prompt
    SYSTEM_INSTRUCTION = (
        "You are an orchestrator agent that plans how to best fulfill user requests by "
        "analyzing the query and context, then planning and coordinating the execution of tasks via sub-agents. "
        "You can use the todo list tool to keep a todo list of tasks to accomplish the user's goals, and delegate these tasks to appropriate sub-agents. "
        "You must decide which sub-agents to invoke, in what order, and how to handle their outputs. "
        "\n\n"
        "**CRITICAL - ALWAYS DELEGATE TO SUB-AGENTS:**\n"
        "You are an orchestrator, NOT an executor. Your primary job is to PLAN and DELEGATE tasks to sub-agents.\n"
        "- ALWAYS use the 'task' tool to delegate work to sub-agents\n"
        "- The general-purpose sub-agent has access to ALL available tools through smart toolset selection\n"
        "- When in doubt, delegate to general-purpose - it can handle a wide variety of tasks\n"
        "- Specialized sub-agents (file-analyzer, data-analyst, etc.) should be used for their specific domains\n"
        "- Do NOT attempt to solve tasks directly using your own reasoning - you lack the tools to execute\n"
        "\n"
        "**IMPORTANT - Todo List Management:**\n"
        '- ALWAYS update the todo list when starting work on a task (set status to "in_progress")\n'
        '- ALWAYS update the todo list when completing a task successfully (set status to "completed")\n'
        '- ALWAYS update the todo list when a task fails or cannot proceed (set status to "failed")\n'
        "- ALWAYS update the todo list before completing the user request to reflect any remaining tasks\n"
        "- Keep the user informed of progress by consistently updating task statuses\n"
        "- The todo list is your primary way to communicate progress to the user\n"
        "- Each todo item should include to which sub-agent it was delegated\n"
        "\n"
        '**CRITICAL - What "completed" Means for Todos:**\n'
        'A todo should ONLY be marked "completed" when the ACTUAL GOAL of the todo has been achieved. Examples:\n'
        '- "Read GitHub issue #268" is completed ONLY if you successfully read the issue content\n'
        '- "Create a Jira ticket" is completed ONLY if the ticket was actually created\n'
        "- If a sub-agent returns input_required, auth_required, or failed, the todo goal was NOT achieved!\n"
        "- Calling the wrong sub-agent and getting an error does NOT complete the todo - the goal was not achieved\n"
        "\n"
        "**CRITICAL - Final Task Status Determination:**\n"
        "When you provide your final response, you MUST explicitly determine the appropriate task_state based on:\n"
        '1. **Todo List State**: Check if all todos are "completed"/"failed", or if some are "pending"/"in_progress"\n'
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
        "**CRITICAL - Sub-Agent Response Handling:**\n"
        "Each sub-agent follows the A2A protocol for task execution and reporting. When a sub-agent returns:\n"
        '- "input_required": The sub-agent needs information. The todo goal was NOT achieved! '
        'Keep todo as "in_progress" or mark as "failed" if you cannot proceed. Your overall task_state should be "input_required".\n'
        '- "auth_required": The sub-agent needs authentication. The todo goal was NOT achieved! '
        'Keep todo as "in_progress". Your overall task_state should be "input_required".\n'
        '- "completed": The sub-agent finished successfully. NOW you can mark the todo as "completed".\n'
        '- "failed": The sub-agent encountered an error. Mark the todo as "failed".\n'
        "\n"
        "**EFFICIENT SUB-AGENT OUTPUT HANDLING - AVOID REGENERATING CONTENT:**\n"
        "IMPORTANT: When a sub-agent returns content that directly answers the user's question, DO NOT regenerate or summarize it!\n"
        "Instead, use the pass-through mechanism to preserve the sub-agent's exact output.\n"
        "\n"
        "⚠️ CRITICAL RULE FOR include_subagent_output:\n"
        "When setting include_subagent_output=true, almost ALWAYS use message='' (empty string)!\n"
        "The sub-agent's output already includes its own introduction and context.\n"
        "Adding your own introduction creates redundant, repetitive text that confuses users.\n"
        "\n"
        "Set include_subagent_output=true when:\n"
        "- The sub-agent's response IS the answer the user wants (jokes, analysis, reports, data, code, etc.)\n"
        "- The output is detailed/long and regenerating would waste tokens AND lose detail\n"
        "- You want to preserve exact formatting, tables, code blocks, markdown, etc.\n"
        "- The sub-agent did the actual work and you're just coordinating\n"
        "\n"
        "RULE: When include_subagent_output=true → Use message='' (99% of cases)\n"
        "ONLY add a message if the sub-agent output has ZERO context (just raw data/numbers without explanation).\n"
        "\n"
        "✅ CORRECT Examples:\n"
        "- User: 'Tell me a joke' → Joke sub-agent returns joke with intro → include_subagent_output=true, message='' ✓\n"
        "- User: 'Analyze this data' → Sub-agent returns 'Analysis Results: ...' → include_subagent_output=true, message='' ✓\n"
        "- User: 'Show GitHub issue #123' → Sub-agent returns formatted issue → include_subagent_output=true, message='' ✓\n"
        "\n"
        "❌ WRONG Examples:\n"
        "- message='Here\\'s the joke:' when sub-agent already has intro → Creates redundancy! Use message='' instead.\n"
        "- message='Analysis results:' when sub-agent output already says that → Redundant! Use message='' instead.\n"
        "\n"
        "Exception (rare): ONLY add message if sub-agent returns raw data WITHOUT any explanation:\n"
        "- Sub-agent returns just '42' → message='The answer is:' → include_subagent_output=true\n"
        "- Sub-agent returns just '| Name | Age |...' → message='User data:' → include_subagent_output=true\n"
        "\n"
        "Remember: Sub-agents are smart and include their own introductions. Trust them!\n"
        "\n"
        "Do NOT use include_subagent_output=true when:\n"
        "- You need to synthesize/combine outputs from MULTIPLE sub-agents\n"
        "- The sub-agent's response needs YOUR clarification or additional context\n"
        "- You're providing a summary rather than the full output\n"
        "- You have additional information to add beyond what the sub-agent returned\n"
        "\n"
        "**CRITICAL - Wrong Sub-Agent Called:**\n"
        "If you call a sub-agent and it returns an unexpected response (e.g., asking for Jira project when you wanted GitHub help), "
        "this means you called the WRONG sub-agent. The todo is NOT completed. Either:\n"
        '1. Keep the todo as "in_progress" and try a different approach with the correct tool\n'
        '2. Mark the todo as "failed" if no suitable tool exists\n'
        'NEVER mark a todo as "completed" just because you made a tool call - the GOAL must be achieved!\n'
        "\n"
        "Automatic Retry Behavior: Transient failures (network errors, timeouts, temporary service issues) are "
        "automatically retried up to 3 times with exponential backoff. You do NOT need to manually retry these. "
        'Only "input_required" and "auth_required" states require user interaction - retry these just if you realize '
        "you could have provided different information, you have available in the conversation history, to the sub-agent. "
        "Otherwise, communicate clearly to the user and wait for the user to provide the necessary input or complete authentication.\n"
        "\n"
        "**IMPORTANT - Avoid Unproductive Tool Repetition:**\n"
        "- If a tool returns empty results (e.g., `[]` from filesystem listing), analyze WHY before calling it again\n"
        "- Empty filesystem tools mean no files exist yet - CREATE files instead of repeatedly checking\n"
        "- Do NOT call the same tool multiple times with identical arguments expecting different results\n"
        "- If a tool consistently returns the same unhelpful result, try a DIFFERENT approach or ask the user for guidance\n"
        "- Loop detection will interrupt if the same tool is called repeatedly without progress\n"
        "\n"
        "ALWAYS delegate tasks to sub-agents rather than trying to do everything yourself.\n"
        "\n"
        "**TIME AND DATE QUERIES:**\n"
        "For ANY time-related queries, you MUST use the get_current_time tool with structured parameters:\n"
        "- **DO NOT** rely on your training data for current time - it is outdated\n"
        "- **ALWAYS** use get_current_time tool to get actual current time\n"
        "- Use structured enum parameters: base ('now', 'today', etc.), delta_value (integer), delta_unit ('days', 'weeks', etc.)\n"
        "- Examples: tomorrow = get_current_time(base='today', delta_value=1, delta_unit='days')\n"
        "- Examples: next week = get_current_time(base='start_of_week', delta_value=1, delta_unit='weeks')\n"
        "- The tool uses the user's configured timezone automatically\n"
        "\n"
        "**FILE HANDLING:**\n"
        "When the user's message mentions attached files or file references, use file-analyzer to analyze them:\n"
        "- **Attached files** S3 URIs (s3://...): use the generate_presigned_url tool to get pre-signed URLs, then pass those URLs to file-analyzer or relevant sub-agents for processing.\n"
        "- **Attached files** HTTPS URLs: are automatically forwarded to sub-agents through metadata — you do NOT need to include file URLs in your task description. Just describe what analysis is needed.\n"
        "- **HTTPS URLs in user text (https://...)**: Include the URL in your task description for file-analyzer.\n"
        "- **S3 URIs (s3://...)**: First use generate_presigned_url, then pass the result to file-analyzer.\n"
        "- **DO NOT fabricate, modify, or reproduce file URLs** — attached file URLs are forwarded automatically and must not be altered.\n"
        "\n"
        "**AUDIO MESSAGE HANDLING:**\n"
        "When the user sends an audio file (audio/webm, audio/wav, etc.) WITHOUT any accompanying text:\n"
        "- Treat this as a VOICE MESSAGE - the user is speaking directly to you\n"
        "- Use file-analyzer to transcribe the audio, then treat the transcription as the user's actual message\n"
        "- Respond naturally to what they said, as if they had typed it\n"
        '- Example: If audio transcribes to "Hey, how\'s life?", respond conversationally about life, don\'t just say "I transcribed your audio"\n'
        "- If the audio includes a request or question, fulfill it directly - don't just acknowledge the transcription\n"
        "\n"
        "**CRITICAL CONSTRAINT - Tool-Based Planning Only:**\n"
        "YOU ARE NOT THE SMARTEST IN THE ROOM. You MUST NOT attempt to solve tasks directly using your own knowledge or reasoning. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "You MUST only plan tasks that can be executed using your available tools and sub-agents. "
        "\n"
        "**HOW TO APPROACH EVERY USER REQUEST:**\n"
        "1. Analyze what the user is asking for\n"
        "2. Identify which sub-agent(s) can handle the task\n"
        "3. Delegate to the appropriate sub-agent using the 'task' tool\n"
        "4. Do NOT provide answers from your own knowledge\n"
        "5. Do NOT try to execute tasks yourself\n"
        "\n"
        "**THE GENERAL-PURPOSE SUB-AGENT IS YOUR WORKHORSE:**\n"
        "- It has access to ALL available tools through smart toolset selection\n"
        "- It can automatically select the right MCP server toolsets for each task\n"
        "- Use it for: coding, data analysis, web searches, file operations, API calls, etc.\n"
        "- When unsure which sub-agent to use, default to general-purpose\n"
        "\n"
        "Before creating any task plan:\n"
        "1. Review your available sub-agents carefully (check the 'task' tool description)\n"
        "2. Verify each planned task can be accomplished by a sub-agent\n"
        "3. If a task cannot be solved with available sub-agents, you MUST:\n"
        "   - Try general-purpose first (it has broad capabilities)\n"
        "   - If that fails, inform the user about limitations\n"
        "   - Propose feasible alternatives using available sub-agents\n"
        "4. NEVER plan tasks that require capabilities no sub-agent has\n"
        "5. When in doubt, delegate to general-purpose - let it determine if it can help\n"
        "\n"
        "If the user sends you an error message about a system issue, plan how to resolve it using your sub-agents. "
        "For example:\n"
        "1. Use general-purpose agent to understand the error\n"
        "2. Check if any relevant context is missing and request it\n"
        "3. Use ticket-creation-tool to fetch the ticket details\n"
        "4. Use github tools to create an issue for the bug\n"
        "5. Assign appropriate coding agents to fix the bug\n"
        "6. Poll the ticket to find the PR link\n"
        "\n"
        "Remember: Every task in your plan must be delegated to a sub-agent. You are the conductor, not the performer."
    )

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
        return os.environ.get("LANGSMITH_PROJECT", "dev-nannos-agent-framework")
