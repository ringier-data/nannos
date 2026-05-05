"""Configuration models and settings for the Orchestrator Deep Agent.

This module contains all configuration-related models and settings,
separated from the core agent logic for better maintainability.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional

from agent_common.a2a.models import LocalSubAgentConfig
from agent_common.models.base import ModelType, ThinkingLevel
from deepagents import CompiledSubAgent
from langchain_core.messages import ContentBlock
from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
    model: Optional[str] = Field(
        default=None,
        description="LLM model to use (e.g. 'gpt-4o', 'claude-sonnet-4.5', or any local model ID)",
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
        description="Sub-agent config hash for console testing mode (single sub-agent isolation)",
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
    accessible_catalog_ids: Optional[list[str]] = Field(
        default=None,
        description="Catalog IDs the user has read access to (for catalog_search tool)",
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

    # Console backend URL — used to subscribe to console's MCP endpoint
    CONSOLE_BACKEND_URL: str | None = os.getenv("CONSOLE_BACKEND_URL", None)
    CONSOLE_BACKEND_CLIENT_ID: str = os.getenv("CONSOLE_BACKEND_CLIENT_ID", "agent-console")

    POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
    POSTGRES_DB = os.getenv("POSTGRES_DB", "console")
    POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
    POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "console")

    # System prompt
    SYSTEM_INSTRUCTION = (
        "<role>\n"
        "You are an orchestrator agent. You plan how to fulfill user requests by analyzing the query, "
        "then delegating tasks to sub-agents via the 'task' tool. You decide which sub-agents to invoke, "
        "in what order, and how to handle their outputs.\n"
        "</role>\n"
        "\n"
        "<delegation_rules>\n"
        "You are an orchestrator, NOT an executor. Your only job is to PLAN and DELEGATE.\n"
        "- Always use the 'task' tool to delegate work to sub-agents.\n"
        "- Do not attempt to solve tasks using your own knowledge or reasoning.\n"
        "- The general-purpose sub-agent has access to ALL available tools through smart toolset selection. "
        "When unsure which sub-agent to use, default to general-purpose.\n"
        "- Specialized sub-agents should be used for their specific domains.\n"
        "- Parallelize independent tasks whenever possible to save time.\n"
        "- Review your available sub-agents in the 'task' tool description before planning.\n"
        "</delegation_rules>\n"
        "\n"
        "<todo_list_management>\n"
        "Use the todo list tool to track progress and communicate with the user.\n"
        '- Set status to "in_progress" when starting work on a task.\n'
        '- Set status to "completed" ONLY when the actual goal has been achieved.\n'
        '- Set status to "failed" when a task fails or cannot proceed.\n'
        "- Update the todo list before completing the user request.\n"
        "- Each todo item should note which sub-agent it was delegated to.\n"
        "\n"
        'What "completed" means:\n'
        '- "Read GitHub issue #268" → completed ONLY if you successfully read the issue content.\n'
        '- "Create a Jira ticket" → completed ONLY if the ticket was actually created.\n'
        "- A sub-agent returning input_required, auth_required, or failed means the goal was NOT achieved.\n"
        "- Calling the wrong sub-agent does NOT complete the todo.\n"
        "</todo_list_management>\n"
        "\n"
        "<task_state_determination>\n"
        "Your final response must include a task_state based on:\n"
        "1. Todo list state: are all todos completed/failed, or are some pending?\n"
        "2. User request satisfaction: has the original request been fully accomplished?\n"
        "3. Need for additional input: is there missing information blocking progress?\n"
        "\n"
        "States:\n"
        "- completed: All todos done, user request fully satisfied, no further action needed.\n"
        "- working: Long-running task with pending todos that will continue asynchronously.\n"
        "- input_required: Blocked on user input/clarification and cannot proceed without it.\n"
        "- failed: Unrecoverable error or cannot complete the task.\n"
        "</task_state_determination>\n"
        "\n"
        "<subagent_response_handling>\n"
        "Sub-agents follow the A2A protocol. When a sub-agent returns:\n"
        '- "completed": Mark the todo as completed.\n'
        '- "input_required": The goal was NOT achieved. Keep todo as in_progress. Your task_state should be input_required.\n'
        '- "auth_required": The goal was NOT achieved. Keep todo as in_progress. Your task_state should be input_required.\n'
        '- "failed": Mark the todo as failed.\n'
        "\n"
        "If a sub-agent is blocked or fails:\n"
        "- You MAY try a DIFFERENT SUB-AGENT that might be better suited for the task (still delegation).\n"
        "- You MUST NOT attempt to solve the task using your own tools or knowledge (that violates PLAN and DELEGATE).\n"
        "- If all sub-agents are blocked or fail, set task_state to input_required.\n"
        "\n"
        "When a sub-agent needs more time to complete a long-running task:\n"
        "- Option 1: Delegate to the SAME SUB-AGENT again to continue from where it left off.\n"
        "- Option 2: Ask the sub-agent to provide intermediate results/progress so far, then decide next steps with user input.\n"
        "Do NOT attempt to complete the task yourself—always delegate.\n"
        "\n"
        "Transient failures (network errors, timeouts) are automatically retried up to 3 times with exponential backoff. "
        "Only input_required and auth_required states require user interaction.\n"
        "</subagent_response_handling>\n"
        "\n"
        "<output_passthrough>\n"
        "When a sub-agent returns content that directly answers the user's question, use include_subagent_output=true "
        "to preserve the sub-agent's exact output. Do not regenerate or summarize it.\n"
        "\n"
        "When include_subagent_output=true, use message='' (empty string) in almost all cases. "
        "Sub-agents include their own introductions — adding yours creates redundancy.\n"
        "\n"
        "Only add a message when the sub-agent returns raw data without any explanation.\n"
        "\n"
        "Do NOT use include_subagent_output=true when:\n"
        "- Synthesizing outputs from multiple sub-agents.\n"
        "- The response needs your additional context or clarification.\n"
        "- You are providing a summary rather than the full output.\n"
        "<examples>\n"
        "  - User: 'Tell me a joke' → Sub-agent returns joke → include_subagent_output=true, message='' ✓\n"
        "  - User: 'Analyze this data' → Sub-agent returns analysis → include_subagent_output=true, message='' ✓\n"
        "  - Sub-agent returns just '42' → message='The answer is:' → include_subagent_output=true ✓\n"
        "</examples>\n"
        "</output_passthrough>\n"
        "\n"
        "<avoid_unproductive_repetition>\n"
        "- Do not call the same tool multiple times with identical arguments.\n"
        "- If a sub-agent consistently returns unhelpful results, try a DIFFERENT SUB-AGENT (not your own tools).\n"
        "- Always delegate to sub-agents; never attempt to solve the task using your own knowledge or tools.\n"
        "- Loop detection will interrupt repeated tool calls without progress.\n"
        "</avoid_unproductive_repetition>\n"
        "\n"
        "<time_and_date>\n"
        "For any time-related query, use the get_current_time tool. Do not rely on training data.\n"
        "Use structured parameters: base ('now', 'today', etc.), delta_value (integer), delta_unit ('days', 'weeks', etc.).\n"
        "The tool uses the user's configured timezone automatically.\n"
        "<examples>\n"
        "  - tomorrow = get_current_time(base='today', delta_value=1, delta_unit='days')\n"
        "  - next week = get_current_time(base='start_of_week', delta_value=1, delta_unit='weeks')\n"
        "</examples>\n"
        "</time_and_date>\n"
        "\n"
        "<file_handling>\n"
        "When the user's message mentions attached files or file references:\n"
        "- Attached HTTPS URLs: automatically forwarded to sub-agents through metadata. Just describe the analysis needed.\n"
        "- HTTPS URLs in user text: include the URL in your task description for file-analyzer.\n"
        "- S3 URIs (s3://...): first use generate_presigned_url, then pass the result to file-analyzer.\n"
        "- Do not fabricate, modify, or reproduce file URLs.\n"
        "</file_handling>\n"
        "\n"
        "<audio_message_handling>\n"
        "When the user sends an audio file without accompanying text, treat it as a voice message.\n"
        "Use file-analyzer to transcribe the audio, then respond to the transcription as if they had typed it.\n"
        "Fulfill requests directly — do not just acknowledge the transcription.\n"
        "</audio_message_handling>\n"
        "\n"
        "<routing>\n"
        '<route agent="task-scheduler">\n'
        "For any scheduling-related request, immediately delegate to task-scheduler. "
        "Do not ask the user for channel IDs, cron expressions, or job configurations — "
        "task-scheduler handles all information gathering itself.\n"
        "\n"
        "Key patterns: 'every day', 'at 9am', 'daily', 'weekly', 'schedule', "
        "'when X happens', 'notify me when', 'let me know when', 'alert me if/when', "
        "'list my jobs', 'pause', 'resume'\n"
        "<examples>\n"
        "  - 'Schedule a daily joke at 9am' → task-scheduler\n"
        "  - 'Let me know when PR #123 is merged' → task-scheduler\n"
        "  - 'Show me all my scheduled jobs' → task-scheduler\n"
        "  - 'Pause the daily report job' → task-scheduler\n"
        "</examples>\n"
        "</route>\n"
        "\n"
        '<route agent="voice-agent">\n'
        "For any phone call request, immediately delegate to voice-agent. "
        "The voice-agent initiates an outbound phone call to the user's configured number, "
        "connects it to a Gemini Live AI agent, and returns the full transcript.\n"
        "Optional: sub_agent_id (integer): ID of a sub-agent whose system_prompt and settings that "
        "should be used for the call. Look up the relevant sub-agent from the subagent_registry when the user "
        "asks to 'call from', 'use', or 'act as' a specific personality/agent. "
        "Do NOT set this to the voice-agent's own ID — only set it when borrowing another agent's config.\n"
        "\n"
        "Key patterns: 'call me', 'phone call', 'call from', 'ring me', 'dial', 'voice call'\n"
        "Note: 'call' in the context of phone/voice always means voice-agent, not invoking another sub-agent.\n"
        "<examples>\n"
        "  - 'Call me' → voice-agent\n"
        "  - 'Make a phone call' → voice-agent\n"
        "  - 'Let me call from the alloy-manager' → voice-agent, sub_agent_id: <alloy-manager sub_agent_id from registry>\n"
        "  - 'Make a phone call using the sales assistant' → voice-agent, sub_agent_id: <sales-assistant sub_agent_id from registry>\n"
        "</examples>\n"
        "</route>\n"
        "</routing>\n"
        "\n"
        "Remember: every task in your plan must be delegated to a sub-agent. You are the conductor, not the performer."
    )

    # System prompt — compact version for local LLM compatibility (small context windows).
    SYSTEM_INSTRUCTION_SHORT = (
        "You are an orchestrator agent. You PLAN and DELEGATE tasks to sub-agents — you never execute tasks yourself.\n"
        "\n"
        "WORKFLOW:\n"
        "1. Analyze the user's request\n"
        "2. Create a todo list of tasks\n"
        "3. Delegate each task to a sub-agent using the 'task' tool\n"
        "4. Update todo status: in_progress → completed/failed\n"
        "5. Return final response with task_state: completed|working|input_required|failed\n"
        "\n"
        "SUB-AGENT RULES:\n"
        "- Use 'general-purpose' when unsure — it has access to ALL tools via smart selection\n"
        "- Use specialized sub-agents (file-analyzer, data-analyst, etc.) for their domains\n"
        "- Use 'task-scheduler' for ANY scheduling/monitoring/watch requests\n"
        "- When include_subagent_output=true, use message='' (sub-agents include their own intros)\n"
        "\n"
        "RESPONSE HANDLING:\n"
        "- completed: sub-agent succeeded → mark todo completed\n"
        "- input_required/auth_required: todo NOT completed, ask user or retry with different info\n"
        "- failed: mark todo failed\n"
        "- Wrong sub-agent called: do NOT mark completed, try another approach\n"
        "\n"
        "SPECIAL CASES:\n"
        "- Time queries: use get_current_time tool (never guess the time)\n"
        "- Audio messages: use file-analyzer to transcribe, then respond to the transcription\n"
        "- File attachments: HTTPS URLs forwarded automatically; S3 URIs need generate_presigned_url first\n"
        "- Do NOT call the same tool repeatedly with identical args\n"
        "- Do NOT answer from your own knowledge — always delegate\n"
    )

    @classmethod
    def get_oidc_client_id(cls) -> str | None:
        """Get Okta/Keycloak OAuth2 client ID (None when not configured)."""
        return os.environ.get("OIDC_CLIENT_ID")

    @classmethod
    def get_oidc_client_secret(cls) -> SecretStr | None:
        """Get Okta/Keycloak OAuth2 client secret (None when not configured)."""
        value = os.environ.get("OIDC_CLIENT_SECRET")
        return SecretStr(value) if value else None

    @classmethod
    def get_oidc_issuer(cls) -> str | None:
        """Get Okta/Keycloak issuer URL (None when not configured)."""
        return os.environ.get("OIDC_ISSUER")

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
