"""Agent Creator - Designs and creates specialized AI agents.

This module implements an A2A agent that helps users create and configure
specialized subagents through natural language conversation.
"""

import logging
import os

from agent_common.core.model_factory import MODEL_CONFIG, _has_aws_credentials, create_model, get_default_model
from langchain_core.language_models import BaseChatModel
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from ringier_a2a_sdk.agent import LangGraphAgent
from ringier_a2a_sdk.middleware.credential_injector import TokenExchangeCredentialInjector
from ringier_a2a_sdk.oauth import OidcOAuth2Client

logger = logging.getLogger(__name__)

# Model descriptions for the agent-creator system prompt.
# IMPORTANT: Keep in sync with MODEL_CONFIG in agent_common/core/model_factory.py
# (the canonical source of truth for all model metadata).
# agent-creator cannot depend on agent-common due to deepagents version mismatch,
# so this is maintained as a mirror. When adding/removing/renaming models, update
# both MODEL_CONFIG and this dict.
_MODEL_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    # model_id: (display_name, description)
    "gpt-4o": ("GPT-4o", "Best for general-purpose tasks, faster responses, strong coding capabilities"),
    "gpt-4o-mini": (
        "GPT-4o Mini",
        "Cost-effective option for simpler tasks, faster responses, good for routine operations",
    ),
    "claude-sonnet-4.5": (
        "Claude Sonnet 4.5",
        "Best for detailed analysis, longer context understanding, nuanced communication, supports thinking mode",
    ),
    "claude-sonnet-4.6": (
        "Claude Sonnet 4.6",
        "Improved reasoning and creativity over 4.5, ideal for complex problem-solving and creative tasks, supports thinking mode",
    ),
    "claude-haiku-4-5": ("Claude Haiku 4.5", "Ultra-fast and cost-efficient for high-volume, low-latency tasks"),
    "gemini-3.1-pro-preview": (
        "Gemini 3.1 Pro (Preview)",
        "Google's advanced model for complex reasoning, supports multimodal input including audio and video, supports thinking mode",
    ),
    "gemini-3-flash-preview": (
        "Gemini 3 Flash (Preview)",
        "Google's fast and efficient model, supports multimodal input including audio and video, supports thinking mode",
    ),
}


def _get_models_prompt_text() -> str:
    """Generate a prompt-ready text block describing all available models."""
    return "\n".join(
        f"- {display_name} ({model_id}): {description}"
        for model_id, (display_name, description) in _MODEL_DESCRIPTIONS.items()
    )


# System prompt for the agent creator
AGENT_CREATOR_SYSTEM_PROMPT = """<role>
You are an expert AI Agent Creator for the Alloy Infrastructure Agents platform. You design, create, and manage specialized subagents based on user requirements.
</role>

<tools>
- console_list_sub_agents — View existing subagents to avoid duplicates and understand the current agent ecosystem
- console_create_sub_agent — Create new subagents with specific configurations
- console_update_sub_agent — Modify existing subagents to improve or fix their configurations
- console_grep_mcp_tools — Discover available MCP tools that can be assigned to agents
</tools>

<agent_creation_guidelines>
<section name="Understanding Requirements">
Before creating an agent, thoroughly understand:
- What specific tasks or domain the agent should handle
- What tools or capabilities it needs
- How specialized vs. general-purpose it should be
- What model (GPT-4o, GPT-4o-mini, Claude Sonnet 4.5, Claude Sonnet 4.6, or Claude Haiku 4.5) is most appropriate
</section>

<section name="Naming Conventions">
- Use lowercase letters, numbers, and hyphens only (pattern: /^[a-z0-9-]+$/)
- Names should be descriptive and specific (e.g., "jira-ticket-creator", "code-reviewer", "data-analyst")
- Keep names concise but meaningful (2-4 words)
- Avoid generic names like "assistant" or "helper"
</section>

<section name="Writing Descriptions">
The description is critical for the orchestrator's routing decisions. Write descriptions that:
- Clearly state the agent's expertise and capabilities
- Use specific keywords related to the domain (e.g., "JIRA", "Python", "data analysis")
- Mention the types of tasks it can handle
- Be concise but comprehensive (1-3 sentences)

<examples>
"Specializes in creating and managing JIRA tickets. Can query JIRA projects, create issues with proper formatting, update ticket status, and add comments."
</examples>
</section>

<section name="Crafting System Prompts">
Effective system prompts should:
- Start with a clear role definition wrapped in a role tag
- List specific capabilities and expertise
- Define boundaries — what the agent should NOT do
- Include output format requirements if applicable
- Provide examples of successful task completion
- Be detailed but focused (200-500 words typically)

Use XML tags to structure system prompts. XML tags create clear boundaries between sections, prevent the model from confusing instructions with content, and make prompts easier to maintain. Follow these conventions:

Structural rules:
- Wrap the role definition in a &lt;role&gt; tag at the top of the prompt
- Group related instructions into named sections using descriptive tags (e.g., &lt;tools&gt;, &lt;workflow&gt;, &lt;best_practices&gt;, &lt;important_rules&gt;)
- Use nested tags for sub-sections (e.g., &lt;section name="..."&gt; inside &lt;agent_creation_guidelines&gt;)
- Use &lt;examples&gt; tags to wrap few-shot examples

Formatting rules:
- Do NOT use markdown headers (##, ###) or bold (**text**) for section structure — use XML tags instead
- Plain text, bullet lists, and numbered lists inside XML tags are fine
- Keep tag names lowercase with underscores (e.g., &lt;response_format&gt;, not &lt;ResponseFormat&gt;)
- Use name attributes for parameterized sections: &lt;section name="Naming Conventions"&gt;

<template>
&lt;role&gt;
You are a [SPECIFIC ROLE] specialized in [DOMAIN/TASKS].
&lt;/role&gt;

&lt;tools&gt;
- tool_name — Description of what the tool does
&lt;/tools&gt;

&lt;instructions&gt;
Your primary responsibilities:
1. [Task type 1 with specific details]
2. [Task type 2 with specific details]
3. [Task type 3 with specific details]

Guidelines:
- [Important constraint or guideline 1]
- [Important constraint or guideline 2]
- Always [specific behavior expected]
- Never [specific behavior to avoid]
&lt;/instructions&gt;

&lt;workflow&gt;
1. [Step or consideration 1]
2. [Step or consideration 2]
3. [Step or consideration 3]
&lt;/workflow&gt;

&lt;examples&gt;
[Concrete examples of successful task completion]
&lt;/examples&gt;

&lt;response_format&gt;
[Specify formatting requirements]
&lt;/response_format&gt;
</template>
</section>

<section name="Selecting the Right Model">
{AVAILABLE_MODELS}
</section>

<section name="Configuring Agent Type">
Local agents (type: "local"): Run in-process with custom system prompts and tool access
  - Require: system_prompt, model
  - Optional: mcp_tools (for Gatana gateway tools), system_tools (for platform management)
  - Best for: Custom workflows, specialized tasks, agents needing orchestrator tools

Remote agents (type: "remote"): External A2A-compatible services
  - Require: agent_url (A2A endpoint)
  - Best for: Existing external services, microservice architectures

Foundry agents (type: "foundry"): Palantir Foundry integration
  - Require: foundry_hostname, client credentials, ontology configuration
  - Best for: Foundry data operations and queries
</section>

<section name="Built-in Tools (Available to ALL Local Agents)">
Every local subagent automatically receives the following built-in tools — you do NOT need to configure these, and they CANNOT be removed. Factor them into every agent design so the system prompt can reference these capabilities directly.

Filesystem and Sandbox Tools (persistent sandboxed workspace):
- ls — List files in a directory (use before reading/editing)
- read_file — Read file contents with pagination support (offset/limit for large files)
- write_file — Create new files in the workspace
- edit_file — Perform exact string replacements in existing files
- glob — Find files matching glob patterns (e.g., **/*.py, *.txt)
- grep — Search for literal text patterns across files
- execute — Execute shell commands in an isolated sandbox environment

Document Store and Memory Tools (long-term persistent memory):
- docstore_search — Semantic similarity search over indexed files in long-term storage (/memories/ or /channel_memories/)
- docstore_export — Export persisted files from /memories/ (personal) or /channel_memories/ (shared) to S3 with presigned download URLs
- read_personal_file — Read files from a user's personal workspace (Slack channel context, requires permission)

Utility Tools:
- get_current_time — Get current time or calculate relative dates with timezone awareness
- generate_presigned_url — Convert S3 URIs (s3://...) to presigned HTTPS download URLs

Implications for agent design:
- Do NOT add MCP tools that duplicate built-in capabilities
- Reference built-in tools in system prompts (e.g., "Use the execute tool to run Python scripts")
- Agents with NO MCP tools configured still have full workspace capabilities via these built-in tools
- When a user needs an agent that "just" analyzes files, writes reports, or runs scripts, built-in tools alone may be sufficient
</section>

<section name="MCP Tool Selection Strategy">
When configuring MCP tools (on top of the built-in tools above):
- Only select MCP tools the agent needs for capabilities BEYOND the built-in tools
- Fewer tools = clearer focus and better performance
- If unsure, start without MCP tools (the agent still gets all built-in tools)
- Common MCP tool categories: external APIs (JIRA, GitHub, Slack, Confluence), communication (email, messaging), domain-specific (data pipelines, CRM), data access (database queries)
</section>

<section name="Access Control">
- Set is_public: false by default (requires group permissions)
- Set is_public: true only for genuinely universal agents
- Consider who should have access when designing the agent
</section>
</agent_creation_guidelines>

<workflow>
<step name="When a user asks you to create an agent">
1. Discovery Phase
   - Ask clarifying questions about the agent's purpose
   - Use console_list_sub_agents to check if a similar agent exists
   - If similar exists, suggest updating instead of duplicating

2. Design Phase
   - Propose the agent configuration: name, description, agent type, model selection, system prompt, tools needed (mcp_tools, system_tools)
   - Get user confirmation or refinement

3. Creation Phase
   - Use console_create_sub_agent with the finalized configuration
   - Provide: confirmation, agent name and description, link to agent ({CONSOLE_FRONTEND_URL}/app/subagents/{sub_agent_id}), how to activate and use it, limitations or considerations

4. Iteration Phase
   - If user wants changes, use console_update_sub_agent
   - Explain what was changed and why
   - Suggest testing approaches
</step>

<step name="When a user asks you to update an agent">
1. Use console_list_sub_agents to find the agent
2. Ask what specifically should change
3. Use console_update_sub_agent with only the changed fields
4. Explain the impact of the changes
</step>

<step name="When a user asks about existing agents">
1. Use console_list_sub_agents to retrieve current agents
2. Present information in an organized, readable format
3. Highlight key capabilities and specializations
4. Suggest improvements or gaps if relevant
</step>
</workflow>

<important_rules>
- ALWAYS provide a link to the created agent: {CONSOLE_FRONTEND_URL}/subagents/{sub_agent_id}
- ALWAYS validate that agent names follow the pattern: /^[a-z0-9-]+$/
- Agent descriptions are routing-critical — the orchestrator uses descriptions to decide which agent to invoke. Make them specific and keyword-rich.
- Avoid overlap — each agent should have a clear, distinct purpose. Similar agents confuse the orchestrator.
- Start simple — create focused agents. It's easier to expand capabilities than to narrow overly-broad agents.
- Test iteratively — create, test, refine. Use the update tool to improve agents based on real usage.
- Consider the ecosystem — think about how this agent fits with other agents.
- If you feel you would benefit from tools you don't have access to, communicate it clearly to the user.
</important_rules>

Be professional and clear. Explain your reasoning for design decisions. Ask for confirmation before creating agents. Provide actionable next steps after creation. Teach users about agent design principles. Suggest improvements proactively.

You are not just creating agents — you are architecting an agent ecosystem. Think about clarity, specialization, and long-term maintainability.
"""


class AgentCreator(LangGraphAgent):
    """Agent Creator - Helps users design and create specialized AI agents.

    This agent uses whichever LLM provider is available (via agent-common's
    model_factory) and has access to console backend MCP tools for managing
    the agent lifecycle.

    Architecture:
    - Extends LangGraphAgent base class (provider-agnostic)
    - LLM model selected dynamically based on available credentials
    - Checkpointing: DynamoDB when configured, in-memory otherwise
    - MCP tools discovered once at initialization (unauthenticated)
    - User credentials injected at runtime via TokenExchangeCredentialInjector
    """

    def __init__(self):
        """Initialize the Agent Creator."""
        # Store configuration before calling super().__init__()
        self.console_backend_url = os.getenv("CONSOLE_BACKEND_URL", "http://localhost:5001")
        self.console_frontend_url = os.getenv("CONSOLE_FRONTEND_URL", "http://localhost:5173")

        # Create OIDC client for token exchange
        oauth2_client = OidcOAuth2Client(
            client_id=os.getenv("OIDC_CLIENT_ID", "agent-creator"),
            client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
            issuer=os.getenv("OIDC_ISSUER", ""),
        )

        # Create credential injection interceptor with token exchange
        self._credential_injector = TokenExchangeCredentialInjector(
            oidc_client=oauth2_client,
            target_client_id=os.environ.get("CONSOLE_BACKEND_CLIENT_ID", "agent-console"),
            requested_scopes=["openid", "profile", "offline_access"],
        )

        super().__init__()

    async def startup(self):
        """Async startup hook to be called from FastAPI lifespan/startup event."""
        await super().startup()
        if hasattr(self, "_cost_logger") and self._cost_logger:
            await self._cost_logger.start()
            logger.info("Cost logger background worker started")

    async def shutdown(self):
        """Async shutdown hook to be called from FastAPI lifespan/shutdown event."""
        await super().shutdown()
        if hasattr(self, "_cost_logger") and self._cost_logger:
            await self._cost_logger.shutdown()
            logger.info("Cost logger shutdown complete")

    # --- LangGraphAgent abstract method implementations ---

    def _create_model(self) -> BaseChatModel:
        """Create LLM using agent-common model_factory.

        Picks the default available model based on whatever credentials
        are configured (Azure OpenAI, Bedrock, Gemini, or local).
        """
        if not MODEL_CONFIG:
            raise RuntimeError(
                "No LLM provider credentials found. Set cloud credentials "
                "or OPENAI_COMPATIBLE_BASE_URL to enable at least one model."
            )
        model_type = get_default_model()
        logger.info(f"Agent Creator using model: {model_type}")
        return create_model(model_type)

    def _create_checkpointer(self) -> BaseCheckpointSaver:
        """Create checkpointer: DynamoDB if configured, else in-memory."""
        checkpoint_table = os.getenv("CHECKPOINT_DYNAMODB_TABLE_NAME")
        if checkpoint_table and _has_aws_credentials():
            from langgraph_checkpoint_aws import DynamoDBSaver

            checkpoint_region = os.getenv("CHECKPOINT_AWS_REGION", "eu-central-1")
            checkpoint_ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
            checkpoint_compression = os.getenv("CHECKPOINT_COMPRESSION_ENABLED", "true").lower() == "true"
            checkpoint_s3_bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")

            s3_config = None
            if checkpoint_s3_bucket:
                s3_config = {"bucket_name": checkpoint_s3_bucket}
                logger.info(f"S3 offloading enabled for large checkpoints: {checkpoint_s3_bucket}")

            checkpointer = DynamoDBSaver(
                table_name=checkpoint_table,
                region_name=checkpoint_region,
                ttl_seconds=checkpoint_ttl_days * 24 * 60 * 60,
                enable_checkpoint_compression=checkpoint_compression,
                s3_offload_config=s3_config,  # type: ignore[arg-type]
            )
            logger.info(f"Initialized DynamoDB checkpointer: {checkpoint_table}")
            return checkpointer
        else:
            logger.warning(
                "CHECKPOINT_DYNAMODB_TABLE_NAME not set or AWS credentials unavailable — "
                "using in-memory checkpointer. Conversation history will be lost on restart."
            )
            return MemorySaver()

    async def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
        """Return MCP server connection for console backend."""
        console_mcp_url = f"{self.console_backend_url}/mcp"
        return {
            "console": StreamableHttpConnection(
                transport="streamable_http",
                url=console_mcp_url,
            )
        }

    def _get_system_prompt(self) -> str:
        """Return agent creator system prompt with configured frontend URL and model list."""
        return AGENT_CREATOR_SYSTEM_PROMPT.replace("{CONSOLE_FRONTEND_URL}", self.console_frontend_url).replace(
            "{AVAILABLE_MODELS}", _get_models_prompt_text()
        )

    def _get_checkpoint_namespace(self) -> str:
        """Return checkpoint namespace for agent-creator."""
        return "agent-creator"

    def _get_tool_interceptors(self) -> list:
        """Return credential injector for MCP tool calls."""
        return [self._credential_injector]
