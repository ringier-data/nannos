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
Users can configure personal sub-agents via console backend with custom prompts and optional
tool whitelists, enabling specialized assistants without deploying separate A2A services.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterable, Callable
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

from deepagents import CompiledSubAgent
from deepagents.backends import StateBackend
from deepagents.backends.composite import CompositeBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_mcp_adapters.callbacks import Callbacks
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.postgres.aio import AsyncPostgresStore
from ringier_a2a_sdk.oauth import OidcOAuth2Client
from ringier_a2a_sdk.utils.mcp_errors import format_mcp_error, is_retryable_mcp_error
from ringier_a2a_sdk.utils.mcp_progress import on_mcp_progress
from ringier_a2a_sdk.utils.streaming import (
    StreamBuffer,
    StructuredResponseStreamer,
    extract_text_from_content,
)

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
from agent_common.backends.attachments_store import (
    build_attachments_backend_from_blocks,
    collect_attachment_blocks_from_messages,
    set_current_attachments_backend,
)
from agent_common.core.graph_utils import (
    build_sub_agent_graph,
    denest_parent_pregel_context,
    isolate_parent_stream_context,
)
from agent_common.core.model_factory import get_model_input_capabilities
from agent_common.middleware.conversation_context_tools_middleware import ContextGatedTool
from agent_common.utils import get_language_display_name

# Tools that are only meaningful in specific conversation contexts. They are NOT
# carried in any static tool list; ConversationContextToolsMiddleware injects them
# only in the contexts listed here. See agent_common/core/CONTEXT.md (D4/D8).
_CONTEXT_GATED_TOOL_RULES: dict[str, frozenset[str]] = {
    "read_personal_file": frozenset({"channel"}),
}

if TYPE_CHECKING:
    from agent_common.backends.attachments_store import AttachmentsStoreBackend
    from agent_common.core.sandbox_pool import SandboxPool
    from agent_common.core.tool_risk_cache import ToolRiskCache
    from agent_common.middleware.conditional_hitl import RiskScorerFn

logger = logging.getLogger(__name__)

# Sub-agent recursion limit — prevents runaway loops when the model ignores
# tool errors and keeps retrying.  Mirrors the orchestrator's MAX_RECURSION_LIMIT.
_SUB_AGENT_RECURSION_LIMIT = int(
    os.getenv(
        "SUB_AGENT_RECURSION_LIMIT",
        os.getenv("MAX_RECURSION_LIMIT", "75"),
    )
)


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
        console_backend_client_id: Optional[str] = None,
        user_id: Optional[str] = None,
        group_ids: Optional[List[str]] = None,
        sandbox_pool: SandboxPool | None = None,
        extra_middlewares: Optional[List[Any]] = None,
        inject_all_tools: Optional[List[BaseTool]] = None,
        risk_scorer: RiskScorerFn | None = None,
        tool_risk_cache: ToolRiskCache | None = None,
        tool_bypass_rules: dict[str, Any] | None = None,
        pending_bypass_rules: list[dict[str, Any]] | None = None,
    ):
        """Initialize the dynamic local agent runnable.

        Args:
            config: Configuration with name, description, system_prompt, mcp_tools
            model: The LangGraph model to use for the agent
            orchestrator_tools: Essential tools always included (get_current_time, docstore, etc.)
            oauth2_client: OAuth2 client for token exchange (required for MCP tool discovery)
            user_token: User's access token for token exchange (required for MCP tool discovery)
            checkpointer: Shared checkpointer for multi-turn conversation state (e.g., PostgreSQLsaver)
            user_name: User's display name for personalization
            user_language: User's preferred language (ISO 639-1 code)
            user_timezone: User's timezone (IANA timezone name)
            custom_prompt: User's custom prompt addendum
            sub_agent_id: Optional sub_agent ID for tracking agent-created agents
            store: Shared AsyncPostgresStore for document storage (enables FilesystemMiddleware persistence)
            backend_factory: Factory function for creating CompositeBackend (for FilesystemMiddleware)
            mcp_gateway_url: MCP gateway URL (defaults to MCP_GATEWAY_URL env var)
            mcp_gateway_client_id: MCP gateway client ID (defaults to MCP_GATEWAY_CLIENT_ID env var)
            console_backend_client_id: Console backend OIDC client ID for token exchange (defaults to CONSOLE_BACKEND_CLIENT_ID env var)
            user_id: User's stable database ID (for playbook loading)
            group_ids: User's group IDs for group playbook loading (all groups)
            extra_middlewares: Optional list of middleware instances to prepend to the standard stack.
            inject_all_tools: Optional pre-discovered tools to use directly (bypasses MCP discovery).
                When set, these tools are used as the agent's MCP tools without gateway discovery.
            risk_scorer: Optional function to score tool calls for conditional HITL.
            tool_risk_cache: Optional cache for tool risk scores to optimize conditional HITL.
            tool_bypass_rules: Optional dict of tool bypass rules for conditional HITL.
            pending_bypass_rules: Optional list of bypass rules that are pending user approval during the current session.
                This object is a reference to UserConfig._pending_bypass_rules and is updated in-place when the user approves bypasses, allowing to collect
                approved bypass rules during execution and persist them after execution finishes.
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
        self.user_id = user_id
        self.group_ids = group_ids
        self.mcp_gateway_url = mcp_gateway_url or os.getenv("MCP_GATEWAY_URL", "")
        self.mcp_gateway_client_id = mcp_gateway_client_id or os.getenv("MCP_GATEWAY_CLIENT_ID", "gatana")
        self.console_backend_client_id = console_backend_client_id or os.getenv(
            "CONSOLE_BACKEND_CLIENT_ID", "agent-console"
        )
        self.console_backend_mcp_url = os.getenv("CONSOLE_BACKEND_MCP_URL", "") or (
            f"{os.getenv('CONSOLE_BACKEND_URL', '')}/mcp" if os.getenv("CONSOLE_BACKEND_URL") else ""
        )
        self.sandbox_pool = sandbox_pool
        self.extra_middlewares = extra_middlewares
        self.inject_all_tools = inject_all_tools
        self._risk_scorer: RiskScorerFn | None = risk_scorer
        self._tool_risk_cache: ToolRiskCache | None = tool_risk_cache
        self._tool_bypass_rules: dict[str, Any] = tool_bypass_rules if tool_bypass_rules is not None else {}
        self._pending_bypass_rules: list[dict[str, Any]] = (
            pending_bypass_rules if pending_bypass_rules is not None else []
        )
        self._agent: CompiledStateGraph | None = None
        self._discovered_tools: Optional[List[BaseTool]] = None
        self._resolved_skills: dict = {}
        # Cached intermediate state for per-invocation sandbox graph rebuild
        self._cached_tools: list[BaseTool] | None = None
        self._cached_context_gated_tools: list[ContextGatedTool] = []
        self._cached_system_prompt: str | None = None
        self._cached_response_format: Any = None
        self._cached_hitl_guarded: dict[str, dict] | None = None
        self._cached_effective_backend_factory: Callable | None = None

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
        """Return PostgreSQL checkpointer for this dynamic agent."""
        return self.checkpointer

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        """Return identifier for cost tracking.

        Uses sub_agent_id if available, otherwise falls back to dynamic-{name}.
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
                f"<language>\n"
                f"Respond in {language_name} ({self.user_language}). "
                f"All responses, explanations, and communications should be in {language_name}. "
                f"Technical terms, code, tool names, and API calls should remain in their original form.\n"
                f"</language>"
            )

        # Timezone preference
        if self.user_timezone:
            preferences_parts.append(
                f"<timezone>\n"
                f"The user's timezone is {self.user_timezone}. "
                f"When using the get_current_time tool, pass timezone='{self.user_timezone}' to get times in their local timezone.\n"
                f"</timezone>"
            )

        # Custom prompt addendum from user settings
        if self.custom_prompt:
            preferences_parts.append(f"<custom_instructions>\n{self.custom_prompt}\n</custom_instructions>")

        if not preferences_parts:
            return ""

        addendum = "\n\n<user_preferences>\n" + "\n".join(preferences_parts) + "\n</user_preferences>"

        logger.debug(
            f"DynamicLocalAgentRunnable: Built preferences addendum for {self.name}: "
            f"language={self.user_language}, timezone={self.user_timezone}, "
            f"custom_prompt={'set' if self.custom_prompt else 'none'}"
        )

        return addendum

    async def _build_playbook_addendum(self) -> str:
        """Build the playbook addendum for the system prompt.

        Reads AGENTS.md from both group and personal scopes, plus builds
        a Skills System block listing all resolved skills (default + group + personal).

        Returns:
            Formatted string to append to the system prompt, or empty string if no playbooks
        """
        if not self.store or not self.user_id:
            return ""

        from agent_common.core.playbook_reader import PlaybookReaderService

        reader = PlaybookReaderService(self.store)
        parts: List[str] = []

        # Load AGENTS.md from group and personal scopes
        group_content, personal_content = await reader.read_agents_md(
            user_id=self.user_id,
            agent_name=self.name,
            group_ids=self.group_ids,
        )

        if group_content:
            parts.append(f"<group_playbook>\n{group_content}\n</group_playbook>")

        if personal_content:
            parts.append(f"<personal_playbook>\n{personal_content}\n</personal_playbook>")

        if group_content and personal_content:
            parts.append(
                "<playbook_conflict_resolution>\n"
                "If the personal playbook contradicts the group playbook, follow the personal playbook.\n"
                "</playbook_conflict_resolution>"
            )

        # Build Skills System block from resolved skills
        if self._resolved_skills:
            skill_lines = []
            for skill in sorted(self._resolved_skills.values(), key=lambda s: s.name):
                scope_label = skill.scope
                if skill.overrides:
                    scope_label += f", overrides {skill.overrides}"
                skill_lines.append(f"- `{skill.name}` ({scope_label}): {skill.description}")

            parts.append(
                "## Skills System\n"
                "You have access to the following skills. Each skill is a directory under /skills/\n"
                "containing a SKILL.md file (and optionally scripts, references, assets).\n\n"
                "To use a skill:\n"
                "1. Match the user's request to a skill description below.\n"
                "2. Read the full SKILL.md with read_file('/skills/<name>/SKILL.md').\n"
                "3. Follow its instructions; read any referenced files as needed.\n\n"
                "Available skills:\n" + "\n".join(skill_lines)
            )

        if not parts:
            return ""

        return "\n\n" + "\n".join(parts)

    def _build_self_improvement_addendum(self) -> str:
        """Build the self-improvement decision tree for the system prompt.

        Guides the agent on when and how to use skill/playbook tools based on
        its effective permission level and the outcome of conversations.

        Returns:
            Formatted string to append to the system prompt, or empty string
        """
        permission = self.config.effective_permission

        # Build scope guidance based on permission level
        if permission == "owner" or permission == "write":
            scope_guidance = (
                "You have **write** access to this agent's configuration.\n"
                "- Use scope='personal' for quick experiments or user-specific preferences.\n"
                "- Use scope='group' to share improvements with your team.\n"
                "- Skills are created in the registry and activated on your agent automatically."
            )
        elif permission == "read":
            scope_guidance = (
                "You have **read-only** access to this agent's default skills.\n"
                "- Use scope='personal' to save skills/playbooks for the current user only.\n"
                "- Use scope='group' to share improvements with the user's team."
            )
        else:
            scope_guidance = (
                "- Use scope='personal' to save skills/playbooks for the current user.\n"
                "- Use scope='group' to share with the user's team."
            )

        return (
            "\n\n<self_improvement>\n"
            "## Self-Improvement Protocol\n\n"
            "After completing a task, evaluate whether you learned something that should be preserved:\n\n"
            "**When to create a skill** (console_create_skill):\n"
            "- You discovered a multi-step procedure that worked well and could be reused\n"
            "- The user taught you a domain-specific workflow or pattern\n"
            "- You built a solution that required non-obvious steps worth documenting\n"
            "- Note: This creates the skill in the registry AND activates it on your agent\n\n"
            "**When to update a skill** (console_update_skill):\n"
            "- An existing skill's instructions were incomplete or incorrect\n"
            "- You found a better approach than what a skill currently describes\n"
            "- The user corrected your behavior on something a skill covers\n"
            "- Note: This updates the registry and self-updates your activation instantly\n\n"
            "**When to activate an existing skill** (console_activate_skill):\n"
            "- You found a skill in the registry (via console_search_skills) that should be active\n"
            "- A previously deactivated skill needs to be re-enabled\n\n"
            "**When to update the playbook** (console_update_playbook):\n"
            "- The user expressed a preference about how you should behave (tone, format, approach)\n"
            "- You learned a constraint or context that affects future interactions\n"
            "- The user corrected a behavioral pattern (not a skill procedure)\n\n"
            "**When NOT to self-improve**:\n"
            "- One-off tasks with no reusable pattern\n"
            "- The user explicitly said not to remember something\n"
            "- The interaction was routine with no new insights\n\n"
            "### CRITICAL: Act, Don't Just Acknowledge\n"
            'When the user gives you feedback like "improve", "do better next time", '
            '"remember this", or corrects your behavior:\n'
            "- **DO** immediately use console_update_playbook or console_create_skill to persist the learning\n"
            '- **DO NOT** merely say "next time I\'ll..." or "I\'ll remember that" — verbal promises are worthless\n'
            "- If you don't persist the improvement with a tool call, you WILL repeat the same mistake\n"
            "- Treat any behavioral correction as a signal to update your playbook NOW\n\n"
            "### Multi-File Skills\n"
            "Skills can include bundled files (scripts, configs, templates) alongside SKILL.md:\n"
            "- Use console_write_skill_file to add/update files in a skill folder.\n"
            "- Use console_delete_skill_file to remove files from a skill folder.\n"
            "- Files are available at /skills/{skill_name}/{file_path} in the sandbox.\n"
            "- Max 20 files per skill, max 256KB per file, max 3 directory levels.\n"
            "- Good candidates for bundled files: validation scripts, config templates, "
            "JSON schemas, example files.\n\n"
            f"### Scope Selection\n{scope_guidance}\n\n"
            "**Important**: Always use the console_* MCP tools for self-improvement.\n"
            "All self-improvement actions require user approval via HITL interrupt.\n"
            "</self_improvement>"
        )

    async def _discover_mcp_tools(self) -> List[BaseTool]:
        """Discover tools from MCP servers with authentication.

        Connects to:
        - Gatana MCP gateway (for standard MCP tools)
        - Console backend MCP (for console_ prefixed tools, if any in whitelist)

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
        mcp_tool_names = set(self.config.mcp_tools or [])

        # Determine which MCP servers to connect to
        has_console_tools = any(name.startswith("console_") for name in mcp_tool_names)
        has_gateway_tools = any(not name.startswith("console_") for name in mcp_tool_names)

        logger.info(f"Discovering MCP tools for {self.name}: gateway={has_gateway_tools}, console={has_console_tools}")

        # Retry parameters
        max_retries = 3
        initial_delay = 1.0
        last_error = None
        delay = initial_delay

        for attempt in range(max_retries):
            try:
                # Build connections dict for MultiServerMCPClient
                connections: dict[str, StreamableHttpConnection] = {}

                # Add Gatana gateway connection if there are non-console tools
                if has_gateway_tools and mcp_gateway_url:
                    # Build auth headers for Gatana gateway
                    headers: dict[str, str] = {}
                    if self.oauth2_client and self.user_token:
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

                    connections[mcp_gateway_client_id] = StreamableHttpConnection(
                        transport="streamable_http",
                        url=mcp_gateway_url,
                        headers=headers if headers else None,
                    )

                # Add console backend MCP connection if there are console_ tools
                if has_console_tools and self.console_backend_mcp_url:
                    console_headers: dict[str, str] = {}
                    if self.oauth2_client and self.user_token:
                        logger.debug(f"Exchanging token for console backend access for {self.name}")
                        console_token = await self.oauth2_client.exchange_token(
                            subject_token=self.user_token,
                            target_client_id=self.console_backend_client_id,
                            requested_scopes=["openid", "profile", "offline_access"],
                        )
                        console_headers["Authorization"] = f"Bearer {console_token}"
                        logger.info(f"Successfully exchanged token for console backend ({self.name})")
                    elif self.user_token:
                        console_headers["Authorization"] = f"Bearer {self.user_token}"
                    connections["console"] = StreamableHttpConnection(
                        transport="streamable_http",
                        url=self.console_backend_mcp_url,
                        headers=console_headers if console_headers else None,
                    )
                elif has_console_tools and not self.console_backend_mcp_url:
                    logger.warning(
                        f"Console MCP tools requested for {self.name} but CONSOLE_BACKEND_URL not configured"
                    )

                if not connections:
                    logger.warning(f"No MCP connections to establish for {self.name}")
                    return []

                client = MultiServerMCPClient(
                    connections=connections,
                    callbacks=Callbacks(on_progress=on_mcp_progress),
                )

                tools = await client.get_tools()
                logger.info(f"Discovered {len(tools)} MCP tools for {self.name}")

                tools = [tool for tool in tools if tool.name in mcp_tool_names]
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

    # Tools that sub-agents always get from console-backend MCP for self-improvement
    _CONSOLE_SELF_IMPROVEMENT_TOOLS = frozenset(
        {
            "console_create_skill",
            "console_update_skill",
            "console_remove_skill",
            "console_activate_skill",
            "console_update_playbook",
            "console_write_skill_file",
            "console_delete_skill_file",
            "console_search_skills",
            "console_import_skill",
        }
    )

    def _wrap_with_agent_name(self, tool: BaseTool) -> BaseTool:
        """Wrap a tool to auto-inject agent_name, hiding it from the LLM schema.

        The sub-agent's name is injected automatically so the LLM doesn't
        need to guess or hallucinate it.
        """
        from langchain_core.tools import StructuredTool
        from pydantic import Field as PydanticField
        from pydantic import create_model

        original_coroutine = tool.coroutine
        agent_name = self.name
        auto_inject_fields = {"agent_name"}

        async def wrapped_coroutine(**kwargs):
            kwargs["agent_name"] = agent_name
            return await original_coroutine(**kwargs)

        # Build new schema without auto-injected fields.
        # MCP tools may have a Pydantic model class or a raw dict (JSON schema).
        new_schema = None
        original_schema = tool.args_schema
        if original_schema and isinstance(original_schema, type) and hasattr(original_schema, "model_fields"):
            # Pydantic model class — rebuild without auto-injected fields
            from pydantic_core import PydanticUndefined

            new_fields = {}
            for name, field_info in original_schema.model_fields.items():
                if name in auto_inject_fields:
                    continue
                default = field_info.default if field_info.default is not PydanticUndefined else ...
                new_fields[name] = (
                    field_info.annotation,
                    PydanticField(
                        default=default,
                        description=field_info.description,
                    ),
                )
            new_schema = create_model(f"{original_schema.__name__}Wrapped", **new_fields)
        elif isinstance(original_schema, dict):
            # Raw JSON schema dict — remove auto-injected fields from properties/required
            import copy

            new_schema = copy.deepcopy(original_schema)
            props = new_schema.get("properties", {})
            for field_name in auto_inject_fields:
                props.pop(field_name, None)
            required = new_schema.get("required", [])
            new_required = [r for r in required if r not in auto_inject_fields]
            if new_required != required:
                new_schema["required"] = new_required

        return StructuredTool(
            name=tool.name,
            description=tool.description,
            args_schema=new_schema,
            coroutine=wrapped_coroutine,
            metadata=tool.metadata,
        )

    async def _discover_console_self_improvement_tools(self) -> List[BaseTool]:
        """Discover self-improvement tools from console-backend MCP.

        Always called (independent of config.mcp_tools) so every sub-agent can
        create/update/remove skills and update playbooks via console-backend.

        Only returns the 4 self-improvement tools; other console_* tools are
        excluded (they come via the orchestrator's tool discovery instead).

        Returns:
            List of discovered self-improvement tools, or empty list on failure.
        """
        if not self.console_backend_mcp_url:
            logger.debug(f"No console backend MCP URL configured for {self.name}, skipping self-improvement tools")
            return []

        try:
            console_headers: dict[str, str] = {}
            if self.oauth2_client and self.user_token:
                console_token = await self.oauth2_client.exchange_token(
                    subject_token=self.user_token,
                    target_client_id=self.console_backend_client_id,
                    requested_scopes=["openid", "profile", "offline_access"],
                )
                console_headers["Authorization"] = f"Bearer {console_token}"
            elif self.user_token:
                console_headers["Authorization"] = f"Bearer {self.user_token}"

            client = MultiServerMCPClient(
                connections={
                    "console": StreamableHttpConnection(
                        transport="streamable_http",
                        url=self.console_backend_mcp_url,
                        headers=console_headers if console_headers else None,
                    ),
                },
                callbacks=Callbacks(on_progress=on_mcp_progress),
            )

            tools = await client.get_tools()
            tools = [t for t in tools if t.name in self._CONSOLE_SELF_IMPROVEMENT_TOOLS]
            validated = [_validate_tool_schema(t) for t in tools]

            # Wrap tools to auto-inject agent_name so the LLM doesn't need to provide it
            wrapped = [self._wrap_with_agent_name(t) for t in validated]
            logger.info(f"Discovered {len(wrapped)} console self-improvement tools for {self.name}")
            return wrapped

        except Exception as e:
            logger.warning(
                f"Failed to discover console self-improvement tools for {self.name}: {e}. "
                f"Self-improvement will not be available this session."
            )
            return []

    def _get_effective_tools(self) -> List[BaseTool]:
        """Get the effective tools for this agent.

        Logic:
        - If inject_all_tools is set: use those directly + essential orchestrator tools
        - If mcp_tools is a non-empty list: use discovered tools + essential orchestrator tools
        - Otherwise (None or empty list): only essential orchestrator tools (NO MCP tools)

        Essential tools always included:
        - get_current_time: For temporal awareness
        - docstore_search: For semantic search over indexed documents
        - read_personal_file: For accessing personal workspace files
        - docstore_export: For exporting files to S3
        - create_presigned_url: For creating S3 presigned URLs
        - catalog_search: For searching Google Drive catalogs

        All tools are validated to ensure they have proper OpenAI schema format.

        Returns:
            List of tools to use for the agent
        """
        # Essential orchestrator tools (always included)
        essential_tool_names = [
            "get_current_time",
            "docstore_search",
            "semantic_search_file",
            "docstore_export",
            "create_presigned_url",
            "catalog_search",
        ]
        essential_tools = [tool for tool in self.orchestrator_tools if tool.name in essential_tool_names]

        # If inject_all_tools is set, use those directly (pre-discovered by orchestrator)
        if self.inject_all_tools is not None:
            # Deduplicate: injected tools override essential tools with same name
            injected_names = {t.name for t in self.inject_all_tools}
            unique_essential = [t for t in essential_tools if t.name not in injected_names]
            logger.info(
                f"Using {len(self.inject_all_tools)} injected tools + "
                f"{len(unique_essential)} essential tools for '{self.name}'"
            )
            return list(self.inject_all_tools) + unique_essential

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

    async def _ensure_agent(self) -> None:
        """Resolve tools, skills, prompt, and build the default graph (lazy, once).

        On first call:
        1. If inject_all_tools is set, use those directly (skip gateway discovery)
        2. Else if mcp_tools is set, discover tools from Gatana gateway with whitelist filtering
        3. Otherwise, use orchestrator tools (inheritance)
        4. Resolve skills from store (default + group + personal)
        5. Cache all intermediate state for _build_graph()
        6. Build self._agent ONLY if sandbox is not active (sandbox agents build
           a fresh graph per invocation in _astream_impl)

        After first call, this is a no-op (guarded by _cached_tools sentinel).
        """
        # Already resolved — skip
        if self._cached_tools is not None:
            return

        # Discover MCP tools if whitelist is configured AND no injected tools
        # (inject_all_tools bypasses gateway discovery entirely)
        if self.inject_all_tools is None:
            if self.config.mcp_tools and len(self.config.mcp_tools) > 0 and self._discovered_tools is None:
                self._discovered_tools = await self._discover_mcp_tools()

        tools = self._get_effective_tools()

        # Always discover console self-improvement MCP tools (independent of mcp_tools whitelist).
        # These tools (console_create_skill, console_update_skill, console_remove_skill,
        # console_update_playbook) are needed for all sub-agents to self-improve.
        if self.store and self.console_backend_mcp_url:
            console_tools = await self._discover_console_self_improvement_tools()
            if console_tools:
                # Avoid duplicates if tools were already discovered via mcp_tools whitelist
                existing_names = {t.name for t in tools}
                tools = tools + [t for t in console_tools if t.name not in existing_names]

        # Pre-resolve skills (default + group + personal) for the virtual filesystem
        if self.store and self.user_id:
            from agent_common.core.skills_resolver import resolve_skills_for_agent
            from agent_common.models.skill import SkillDefinition as AgentSkillDef
            from agent_common.models.skill import SkillFile as AgentSkillFile

            def _to_skill_def(s) -> AgentSkillDef:
                if isinstance(s, AgentSkillDef):
                    return s
                if isinstance(s, dict):
                    files = [
                        AgentSkillFile(path=f["path"], content=f.get("content", ""), encoding=f.get("encoding"))
                        if isinstance(f, dict)
                        else f
                        for f in s.get("files", [])
                    ]
                    return AgentSkillDef(
                        name=s.get("name", ""),
                        description=s.get("description", ""),
                        body=s.get("body", ""),
                        files=files,
                    )
                return AgentSkillDef(name=s.name, description=s.description, body=s.body, files=s.files)

            default_skills = []
            if hasattr(self.config, "skills") and self.config.skills:
                default_skills = [_to_skill_def(s) for s in self.config.skills]
            self._resolved_skills = await resolve_skills_for_agent(
                store=self.store,
                user_id=self.user_id,
                agent_name=self.name,
                group_ids=self.group_ids or [],
                default_skills=default_skills,
            )

        logger.info(f"Creating LangGraph agent '{self.name}' with {len(tools)} tools")

        # Route conversation-context-gated tools (e.g. read_personal_file) to the
        # ConversationContextToolsMiddleware instead of binding them statically.
        # The instance is sourced from any available pool; the middleware injects
        # it only in the allowed conversation contexts.
        gated_pool = (
            list(self.orchestrator_tools) + list(self.inject_all_tools or []) + list(self._discovered_tools or [])
        )
        seen_gated: set[str] = set()
        gated_tools: list[ContextGatedTool] = []
        for tool in gated_pool:
            if isinstance(tool, BaseTool) and tool.name in _CONTEXT_GATED_TOOL_RULES and tool.name not in seen_gated:
                seen_gated.add(tool.name)
                gated_tools.append(ContextGatedTool(tool, _CONTEXT_GATED_TOOL_RULES[tool.name]))
        self._cached_context_gated_tools = gated_tools
        # Ensure gated tools are not also statically bound (de-dup safety).
        if seen_gated:
            tools = [t for t in tools if not (isinstance(t, BaseTool) and t.name in seen_gated)]

        # Build system prompt with A2A protocol addendum and user preferences
        system_prompt = self.config.system_prompt + A2A_PROTOCOL_ADDENDUM
        preferences_addendum = self._build_preferences_addendum()
        if preferences_addendum:
            system_prompt += preferences_addendum
            logger.debug(f"Added user preferences addendum to {self.name} system prompt")

        # Load playbooks from persistent store (AGENTS.md auto-loaded, skills indexed)
        playbook_addendum = await self._build_playbook_addendum()
        if playbook_addendum:
            system_prompt += playbook_addendum
            logger.debug(f"Added playbook addendum to {self.name} system prompt")

        # Add self-improvement decision tree (guides agent on when/how to use skill tools)
        self_improvement_addendum = self._build_self_improvement_addendum()
        if self_improvement_addendum:
            system_prompt += self_improvement_addendum
            logger.debug(f"Added self-improvement addendum to {self.name} system prompt")

        # Get model-specific response_format strategy (may mutate tools list for Bedrock+thinking)
        response_format = get_response_format(
            model=self.model,
            tools=tools,
            thinking_enabled=bool(self.config.thinking_level),
        )

        # Build agent via the shared helper: handles backend factory selection
        # (injected vs. auto-created), middleware stack assembly, and graph creation.
        # HITL guards are now managed in the tool_risk_scores DB table and enforced
        # via dynamic risk scoring (base_score=1.0 entries always trigger interrupt).

        # Build the backend factory with resolved skills mounted at /skills/
        effective_backend_factory = self.backend_factory
        if not effective_backend_factory and self._resolved_skills:
            from agent_common.core.graph_utils import create_indexing_backend_factory

            effective_backend_factory = create_indexing_backend_factory(
                store=self.store,
                resolved_skills=self._resolved_skills,
            )
        elif effective_backend_factory and self._resolved_skills:
            # backend_factory already set (e.g., from orchestrator). Replace or add
            # /skills/ route with the sub-agent's own SkillsStoreBackend.
            from agent_common.backends.skills_store import SkillsStoreBackend as _SSB

            if isinstance(effective_backend_factory, CompositeBackend):
                routes = {**effective_backend_factory.routes, "/skills/": _SSB(self._resolved_skills)}
                effective_backend_factory = CompositeBackend(
                    default=effective_backend_factory.default,
                    routes=routes,
                )
            else:
                effective_backend_factory = CompositeBackend(
                    default=effective_backend_factory,
                    routes={"/skills/": _SSB(self._resolved_skills)},
                )

        # Cache intermediate state for _build_graph() (used by both paths)
        self._cached_tools = tools
        self._cached_system_prompt = system_prompt
        self._cached_response_format = response_format
        self._cached_hitl_guarded = None  # Static guards moved to DB (tool_risk_scores table)
        self._cached_effective_backend_factory = effective_backend_factory

        # Only build the default (non-sandbox) graph if sandbox is NOT active.
        # Sandbox-enabled agents build a fresh graph per invocation in _astream_impl()
        # with a sandboxed backend factory, so building one here would be wasteful.
        sandbox_active = getattr(self.config, "sandbox_enabled", False) and self.sandbox_pool is not None
        if not sandbox_active:
            self._agent = self._build_graph(effective_backend_factory)

    def _build_graph(
        self,
        backend_factory: Callable | None = None,
        extra_middlewares: list | None = None,
        extra_tools: list | None = None,
        sandbox_enabled: bool = False,
        sandbox_home: str | None = None,
    ) -> CompiledStateGraph:
        """Build a LangGraph agent from cached state with the given backend factory.

        Single code path for both sandbox and non-sandbox graphs, ensuring
        feature parity (same tools, prompt, middleware, response format).

        Args:
            backend_factory: Backend factory for FilesystemMiddleware.
                For non-sandbox: the indexing backend factory.
                For sandbox: a sandboxed factory wrapping the base factory.
            extra_middlewares: Optional list of additional AgentMiddleware instances
                to prepend to the standard middleware stack (e.g. SkillSandboxSyncMiddleware).
                Combined with self.extra_middlewares (instance-level comes first).
            extra_tools: Optional list of additional tools to include alongside
                the cached tools (e.g. copy_to_sandbox for sandbox-enabled agents).
            sandbox_enabled: When True, configures StoragePathsInstructionMiddleware
                with sandbox-aware instructions.
            sandbox_home: Sandbox home directory path (e.g. "/home/ubuntu").

        Returns:
            A compiled CompiledStateGraph
        """
        # Combine instance-level extra_middlewares with call-level ones
        combined_middlewares = list(self.extra_middlewares or []) + list(extra_middlewares or []) or None

        # Build tool_server_map from tool metadata for server slug resolution
        tool_server_map: dict[str, str] = {}
        for tool in self._cached_tools or []:
            metadata = getattr(tool, "metadata", None)
            if metadata and isinstance(metadata, dict):
                server_name = metadata.get("server_name")
                if server_name:
                    tool_server_map[tool.name] = server_name

        return build_sub_agent_graph(
            model=self.model,
            tools=self._cached_tools or [],
            system_prompt=self._cached_system_prompt or self.config.system_prompt,
            checkpointer=self.checkpointer,
            store=self.store,
            response_format=self._cached_response_format,
            backend_factory=backend_factory or None,
            hitl_guarded_tools=self._cached_hitl_guarded,
            extra_middlewares=combined_middlewares,
            extra_tools=extra_tools,
            sandbox_enabled=sandbox_enabled,
            sandbox_home=sandbox_home,
            risk_scorer=self._risk_scorer,
            tool_risk_cache=self._tool_risk_cache,
            tool_server_map=tool_server_map or None,
            context_gated_tools=self._cached_context_gated_tools or None,
        ).with_config({"recursion_limit": _SUB_AGENT_RECURSION_LIMIT})

    def _build_attachments_backend(self, input_data: SubAgentInput) -> "AttachmentsStoreBackend | None":
        """Build an ephemeral attachments backend from the incoming message.

        Files attached to the conversation arrive as multi-modal content blocks
        (with a presigned ``url`` or inline ``base64``). They are already passed
        to the LLM as content, but to let skills/sandbox commands access them on
        disk we additionally expose them at ``/attachments/{filename}``.

        Returns ``None`` when the message carries no file attachments.
        """
        from agent_common.backends.attachments_store import build_attachments_backend_from_blocks

        try:
            raw_content = input_data.messages[-1].content
        except (AttributeError, IndexError, TypeError):
            return None

        backend = build_attachments_backend_from_blocks(raw_content)
        if backend is not None:
            logger.info("Mounting %d attachment(s) at /attachments/ for '%s'", len(backend._attachments), self.name)
        return backend

    @staticmethod
    def _derive_attachment_filename(
        block: dict, url: str | None, mime_type: str | None, idx: int, used_names: set[str]
    ) -> str:
        """Derive a stable, unique, flat filename for an attachment block."""
        from agent_common.backends.attachments_store import derive_attachment_filename

        return derive_attachment_filename(block, url, mime_type, idx, used_names)

    def _compose_backend_with_attachments(
        self,
        base_factory: Any,
        attachments_backend: "AttachmentsStoreBackend | None",
    ) -> Any:
        """Return a backend factory with the ``/attachments/`` route added.

        When there are no attachments, the base factory is returned unchanged.
        """
        if attachments_backend is None:
            return base_factory

        if isinstance(base_factory, CompositeBackend):
            routes = {**base_factory.routes, "/attachments/": attachments_backend}
            return CompositeBackend(default=base_factory.default, routes=routes)
        if base_factory is None:
            return CompositeBackend(default=StateBackend(), routes={"/attachments/": attachments_backend})
        return CompositeBackend(default=base_factory, routes={"/attachments/": attachments_backend})

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
        # For HITL resume: Command goes directly to the inner graph, skip message extraction
        from langgraph.types import Command as LGCommand

        _is_hitl_resume = isinstance(input_data, LGCommand)

        human_message = None
        if not _is_hitl_resume:
            # Prepare input with multi-modal support (handles content blocks)
            human_message = await self._prepare_human_message_input(input_data)
        context_id, task_id = (None, None) if _is_hitl_resume else self._extract_tracking_ids(input_data)

        # Sandbox lifecycle: acquire per-invocation, release in finally
        pooled_sandbox = None
        sandbox_active = getattr(self.config, "sandbox_enabled", False) and self.sandbox_pool is not None

        # Token for the per-turn attachments context registration (reset in finally).
        _attachments_token = None

        try:
            # Clear per-invocation caches on extra_middlewares (e.g. ToolsetSelectorMiddleware)
            for mw in self.extra_middlewares or []:
                if hasattr(mw, "clear_cache"):
                    mw.clear_cache()

            # Ensure tools/skills/prompt are resolved and default agent is built (lazy init)
            await self._ensure_agent()

            # Build the /attachments/ backend by merging the current message's blocks
            # with any blocks found in the last 20 checkpoint messages.
            # The current message is appended as the newest entry so its blocks win
            # on filename collisions while still preserving files from prior turns.
            # On HITL resume there is no fresh message — checkpoint only.
            base_backend_factory = self._cached_effective_backend_factory or StateBackend()
            # self._agent is None for sandbox agents (_ensure_agent skips building it;
            # the sandbox graph is built later per-invocation).  Any compiled graph for
            # this agent can read the thread's checkpoint, so we fall back to building
            # a temporary non-sandbox graph solely for the aget_state call.
            _graph_for_state = self._agent or self._build_graph(
                self._cached_effective_backend_factory or StateBackend()
            )
            checkpoint_state = await _graph_for_state.aget_state(cast(RunnableConfig, config))
            checkpoint_msgs = list(checkpoint_state.values.get("messages") or [])
            msgs_to_scan = checkpoint_msgs if human_message is None else checkpoint_msgs + [human_message]
            all_blocks = collect_attachment_blocks_from_messages(msgs_to_scan)
            attachments_backend = build_attachments_backend_from_blocks(all_blocks)
            if attachments_backend is not None:
                logger.info(
                    "Mounting %d attachment(s) at /attachments/ for '%s'",
                    len(attachments_backend._attachments),
                    self.name,
                )
            invocation_backend_factory = self._compose_backend_with_attachments(
                base_backend_factory, attachments_backend
            )

            # Register the attachments backend for this turn so tools that read
            # outside the FilesystemMiddleware backend (e.g. semantic_search_file)
            # can reach the attached files. Reset in the finally block.
            if attachments_backend is not None:
                _attachments_token = set_current_attachments_backend(attachments_backend)

            # If sandbox enabled, build a per-invocation graph with sandbox backend
            if sandbox_active:
                session_id = config.get("configurable", {}).get("thread_id", context_id or "unknown")
                pooled_sandbox = await self.sandbox_pool.acquire(session_id, self.name)

                # Build per-invocation sandboxed graph via shared _build_graph()
                from agent_common.core.graph_utils import create_sandboxed_backend_factory
                from agent_common.core.sandbox_tools import create_copy_to_sandbox_tool
                from agent_common.middleware.sandbox_path_hint import SandboxPathHintMiddleware

                sandbox_home = self.sandbox_pool.home or "/home/ubuntu"

                sandboxed_backend_factory = create_sandboxed_backend_factory(
                    sandbox_backend=pooled_sandbox.backend,
                    base_backend=invocation_backend_factory,
                )

                # Create copy_to_sandbox tool with access to virtual FS and sandbox
                copy_tool = create_copy_to_sandbox_tool(
                    composite_backend=invocation_backend_factory,
                    sandbox_backend=pooled_sandbox.backend,
                    sandbox_home=sandbox_home,
                )

                # Assemble sandbox-specific middlewares
                sandbox_middlewares: list = [
                    SandboxPathHintMiddleware(sandbox_home=sandbox_home),
                ]

                # Wire SkillSandboxSyncMiddleware if skills are available
                if self._cached_effective_backend_factory and isinstance(
                    self._cached_effective_backend_factory, CompositeBackend
                ):
                    skills_backend = self._cached_effective_backend_factory.routes.get("/skills/")
                    if skills_backend:
                        from agent_common.middleware.skill_sandbox_sync import SkillSandboxSyncMiddleware

                        sandbox_middlewares.append(
                            SkillSandboxSyncMiddleware(
                                sandbox_backend=pooled_sandbox.backend,
                                skills_backend=skills_backend,
                                skills_hash_ref={},
                                sandbox_home=sandbox_home,
                            )
                        )

                agent = self._build_graph(
                    sandboxed_backend_factory,
                    extra_middlewares=sandbox_middlewares,
                    extra_tools=[copy_tool],
                    sandbox_enabled=True,
                    sandbox_home=sandbox_home,
                )

                logger.info(
                    "Built sandboxed graph for '%s' (session=%s)",
                    self.name,
                    session_id[:8] if session_id else "?",
                )
            else:
                # Non-sandbox: rebuild the graph for this turn only when attachments
                # are present (to mount /attachments/); otherwise reuse cached agent.
                if attachments_backend is not None:
                    agent = self._build_graph(invocation_backend_factory)
                else:
                    agent = self._agent

            agent_input = input_data if human_message is None else {"messages": [human_message]}

            # CRITICAL: Dynamic agent graphs are standalone, not subgraphs.
            # checkpoint_ns must be "" for standalone graphs (same pattern as GPAgentRunnable).
            # Thread isolation is provided by unique thread_id="{context_id}::dynamic-{name}"
            # which the dispatch middleware sets correctly in the config it passes here.
            standalone_config = {
                **config,
                "metadata": {
                    **config.get("metadata", {}),
                    "agent_name": self.name,  # Ensure tools resolve to this agent's skills
                    "has_attachments": attachments_backend is not None,
                },
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

            # Build a lightweight runtime context for the sub-agent graph.
            # This provides tool_bypass_rules so that ConditionalHITLMiddleware can
            # store and check bypass decisions within the same invocation.
            import types

            subagent_context = types.SimpleNamespace(
                tool_bypass_rules=self._tool_bypass_rules,
                tool_risk_cache=self._tool_risk_cache,
                _pending_bypass_rules=self._pending_bypass_rules,
            )

            # Stream the agent with custom events and messages using v2 format
            # v2: every chunk is a StreamPart dict: {"type": ..., "ns": ..., "data": ...}
            #
            # The sub-agent graph is invoked from inside the orchestrator's Pregel
            # node, which leaks the parent's __pregel_task_id into our effective
            # config and would force the sub-agent's Pregel loop to is_nested=True.
            # In that mode a GraphInterrupt propagates as an exception instead of
            # being suppressed + saved to the checkpoint, and the resulting
            # checkpoint does NOT cleanly replay an interrupted tool node when the
            # orchestrator resumes via a standalone Command(resume) (the approved
            # call is lost and the model re-runs). denest_parent_pregel_context()
            # strips that key so the graph runs as a standalone root: interrupts are
            # suppressed + persisted and the post-stream aget_state check below
            # re-raises them. The contextvar must stay active for the whole
            # iteration, so the de-nesting wraps the astream generator itself.
            async def _denested_agent_astream() -> AsyncIterable[Any]:
                # denest_parent_pregel_context: run as a standalone Pregel root.
                # isolate_parent_stream_context: drop the inherited
                # StreamMessagesHandler so this sub-agent's token/tool-call chunks
                # do not leak into the orchestrator's `messages` stream (where they
                # would surface unattributed, e.g. an unprefixed "Using eval…").
                with denest_parent_pregel_context(), isolate_parent_stream_context():
                    async for _part in agent.astream(
                        agent_input,
                        config=standalone_config,
                        stream_mode=["custom", "messages"],
                        context=subagent_context,
                        version="v2",
                    ):
                        yield _part

            async for part in _denested_agent_astream():
                part_type = part["type"]

                # Capture tool calls and stream content from message chunks
                if part_type == "messages":
                    msg_chunk, _metadata = part["data"]
                    if not isinstance(msg_chunk, AIMessageChunk):
                        continue

                    # --- Structured response streaming ---
                    # Tool-call status emission is handled by ToolStatusMiddleware
                    # (emits via stream_writer with complete args).
                    if msg_chunk.tool_call_chunks:
                        for tc_chunk in msg_chunk.tool_call_chunks:
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

                # --- Custom events from middleware (via stream_writer) ---
                if part_type == "custom":
                    event_data = part["data"]
                    if isinstance(event_data, tuple) and len(event_data) == 2:
                        event_type, payload = event_data
                        if isinstance(payload, dict):
                            if event_type == "todo_status" and "todos" in payload:
                                yield TaskUpdate(
                                    event_metadata=WorkPlanMeta(todos=payload["todos"]),
                                )
                                continue
                            status = payload.get("status")
                            if status:
                                yield TaskUpdate(
                                    status_text=status,
                                    event_metadata=ActivityLogMeta(),
                                )
                    elif isinstance(event_data, dict):
                        status = event_data.get("status")
                        if status:
                            yield TaskUpdate(
                                status_text=status,
                                event_metadata=ActivityLogMeta(),
                            )

            # Flush remaining buffer
            remaining = stream_buffer.flush_all()
            if remaining:
                yield ArtifactUpdate(content=remaining)

            # Post-stream interrupt check: with is_nested=False (default for
            # standalone graphs), GraphInterrupt is suppressed inside the Pregel
            # loop and saved to the checkpoint.  We must inspect the post-stream
            # state to detect suppressed interrupts and re-raise them so the
            # orchestrator can surface them to the user.
            logger.info(
                "[DYNAMIC AGENT] Post-stream check: calling aget_state for '%s' (thread_id=%s, checkpoint_ns=%s)",
                self.name,
                standalone_config.get("configurable", {}).get("thread_id", "?"),
                standalone_config.get("configurable", {}).get("checkpoint_ns", "?"),
            )
            post_state = await agent.aget_state(standalone_config)
            logger.info(
                "[DYNAMIC AGENT] Post-stream aget_state result for '%s': has_state=%s, num_tasks=%d, interrupts=%s",
                self.name,
                post_state is not None,
                len(post_state.tasks) if post_state and post_state.tasks else 0,
                [i.value for i in post_state.interrupts] if post_state and post_state.interrupts else "[]",
            )
            if post_state and post_state.interrupts:
                logger.info(
                    "[DYNAMIC AGENT] Post-stream check found %d suppressed interrupt(s) "
                    "in '%s' — re-raising GraphInterrupt",
                    len(post_state.interrupts),
                    self.name,
                )
                raise GraphInterrupt(post_state.interrupts)

            # Retrieve final state (checkpointer saves it after each node)
            final_values = await retrieve_final_state(agent, standalone_config)
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

        finally:
            # Release sandbox back to pool for warm reuse
            if pooled_sandbox is not None and self.sandbox_pool is not None:
                session_id = config.get("configurable", {}).get("thread_id", context_id or "unknown")
                await self.sandbox_pool.release(session_id, self.name)

            # Clear the per-turn attachments context registration.
            if _attachments_token is not None:
                from agent_common.backends.attachments_store import reset_current_attachments_backend

                reset_current_attachments_backend(_attachments_token)

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
    console_backend_client_id: Optional[str] = None,
    user_id: Optional[str] = None,
    group_ids: Optional[List[str]] = None,
    sandbox_pool: SandboxPool | None = None,
    extra_middlewares: Optional[List[Any]] = None,
    inject_all_tools: Optional[List[BaseTool]] = None,
    risk_scorer: RiskScorerFn | None = None,
    tool_risk_cache: ToolRiskCache | None = None,
    tool_bypass_rules: dict[str, Any] | None = None,
    pending_bypass_rules: list[dict[str, Any]] | None = None,
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
        checkpointer: Shared checkpointer for multi-turn conversation state (e.g., PostgreSQLSaver)
        user_name: User's display name for personalization
        user_language: User's preferred language (ISO 639-1 code)
        user_timezone: User's timezone (IANA timezone name)
        custom_prompt: User's custom prompt addendum
        store: Shared AsyncPostgresStore for document storage (enables FilesystemMiddleware persistence)
        backend_factory: Factory function for creating CompositeBackend (for FilesystemMiddleware)
        mcp_gateway_url: MCP gateway URL (defaults to MCP_GATEWAY_URL env var)
        mcp_gateway_client_id: MCP gateway client ID (defaults to MCP_GATEWAY_CLIENT_ID env var)
        console_backend_client_id: Console backend OIDC client ID for token exchange (defaults to CONSOLE_BACKEND_CLIENT_ID env var)
        user_id: User's stable database ID (for playbook loading)
        group_ids: User's group IDs for group playbook loading (all groups)
        extra_middlewares: Optional middleware instances to prepend to the standard stack.
        inject_all_tools: Optional pre-discovered tools (bypasses MCP discovery).
        tool_bypass_rules: User's persisted HITL bypass rules keyed by tool/server.
        pending_bypass_rules: Per-turn pending bypass rules list shared with executor persistence.

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
        console_backend_client_id=console_backend_client_id,
        user_id=user_id,
        group_ids=group_ids,
        sandbox_pool=sandbox_pool,
        extra_middlewares=extra_middlewares,
        inject_all_tools=inject_all_tools,
        risk_scorer=risk_scorer,
        tool_risk_cache=tool_risk_cache,
        tool_bypass_rules=tool_bypass_rules,
        pending_bypass_rules=pending_bypass_rules,
    )

    return CompiledSubAgent(
        name=config.name,
        description=config.description,
        runnable=runnable,  # type: ignore[arg-type]
    )
