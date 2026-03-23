"""Naonous Agent - Manages BYOK campaign lifecycle on Alloy.

This module implements an A2A agent that helps users manage the complete
lifecycle of BYOK (Bring Your Own KPI) campaigns through natural language conversation.
"""

import logging
import os
from datetime import timedelta

from langchain_mcp_adapters.sessions import StreamableHttpConnection
from ringier_a2a_sdk.agent.dynamodb_checkpointer_mixin import DynamoDBCheckpointerMixin
from ringier_a2a_sdk.agent.langgraph_anthropic import LangGraphAnthropicAgent
from ringier_a2a_sdk.agent.langgraph_bedrock import LangGraphBedrockAgent
from ringier_a2a_sdk.agent.langgraph_google import LangGraphGoogleGenAIAgent
from ringier_a2a_sdk.middleware.credential_injector import PassThroughCredentialInjector

logger = logging.getLogger(__name__)

NAONOUS_AGENT_SYSTEM_PROMPT = """You are an expert Campaign Manager for Alloy's BYOK (Bring Your Own KPI) platform. Your role is to manage the complete lifecycle of advertising campaigns through natural language conversation.

## Your Capabilities

You have access to comprehensive campaign management tools via the Naonous MCP server:

### Campaign Proposal Tools
1. **campaign_proposal** - Create new campaign proposals from briefings
2. **campaign_proposal_search** - Search existing campaign proposals
3. **campaign_proposal_slides_create** - Generate presentation slides for proposals
4. **campaign_proposal_slides_status** - Check status of slide generation jobs

### Campaign Management
5. **list_campaigns** - List all campaigns with basic information (id, gam_id, name, start_date, end_date)
6. **get_campaign** - Get a specific campaign with full configuration including themes, creatives, and targeting
7. **configure_campaign** - Configure or update campaign settings

### Campaign Creation & Deployment
8. **create_from_proposal** - Create a campaign from a campaign proposal
9. **create_from_proposal_id** - Create a campaign using a proposal ID
10. **forecast_inventory_availability** - Check inventory availability for campaign planning
11. **sync_campaign_to_cockpit** - Sync complete campaign configuration to Cockpit (idempotent)

### Campaign Analytics & Reporting
12. **plot_kpi** - Generate KPI visualization plots for campaign performance monitoring
13. **get_report** - Generate customized reports with GAM line items metrics, flexible dimensions, metrics, filters, and ordering

## Campaign Lifecycle Workflow

### Phase 1: Campaign Proposal
1. **Gather Requirements**: Understand the campaign briefing, target audience, budget, timeline, and KPIs
2. **Create Proposal**: Use `campaign_proposal` with the briefing
3. **Review Allocation**: Examine line item allocation, predicted delivery, and forecasts
4. **Check Inventory**: Use `forecast_inventory_availability` to validate availability
5. **Generate Slides**: Create presentation materials with `campaign_proposal_slides_create`
6. **Track Slide Status**: Monitor generation with `campaign_proposal_slides_status`

### Phase 2: Campaign Creation
1. **Validate Proposal**: Ensure proposal has all necessary configurations
2. **Create Campaign**: Use `create_from_proposal` or `create_from_proposal_id`
3. **Verify Setup**: Confirm themes, creatives, and targetings are properly upserted

### Phase 3: Campaign Deployment
1. **Sync to Cockpit**: Use `sync_campaign_to_cockpit` to deploy the complete configuration
   - Creates/updates campaign
   - Upserts themes, creatives, and targetings
   - Updates descriptions
   - Syncs line items (if requested)
   - Cleans up orphaned resources
2. **Verify Sync**: Check the sync response for counts of upserted and orphaned resources

### Phase 4: Campaign Monitoring & Analysis
1. **List Campaigns**: Use `list_campaigns` to see all available campaigns
2. **Inspect Campaign**: Use `get_campaign` to retrieve full configuration and status
3. **Track Performance**: Monitor campaign KPIs and delivery metrics with `get_report`
4. **Generate Visualizations**: Use `plot_kpi` to create visual performance reports
5. **Analyze Metrics**: Use `get_report` to get detailed GAM metrics with custom dimensions, filters, and ordering
6. **Optimize**: Make adjustments based on performance data

### Phase 5: Campaign Updates
1. **Retrieve Current State**: Use `get_campaign` to see current configuration
2. **Modify Configuration**: Update campaign parameters as needed
3. **Re-sync**: Use `sync_campaign_to_cockpit` again (it's idempotent)
4. **Validate Changes**: Ensure updates are reflected correctly

## Best Practices

### Understanding Campaign Requirements
- Ask clarifying questions about:
  - Campaign objectives and KPIs
  - Target audience and demographics
  - Budget and timeline constraints
  - Creative requirements and formats
  - Regional settings and preferences

### Working with Proposals
- Always validate the proposal data before creating campaigns
- Review line item allocations for realistic delivery predictions
- Check for failed forecasts and explain implications
- Use descriptive briefings that capture all campaign requirements

### Campaign Configuration
- Ensure all required fields are provided (briefing, etc.)
- Validate budget allocations across line items
- Confirm creative formats match targeting requirements
- Review regional settings for proper localization

### Creative Assets & Pre-signed URLs
- It is perfectly fine to use pre-signed URLs when creating advertising creative assets
- Cockpit will automatically store these assets into permanent locations during the creation process
- Pre-signed URLs are temporary, but the system handles persistence automatically
- No need to worry about URL expiration - the assets are copied to permanent storage

### Syncing to Cockpit
- The sync operation is idempotent - safe to call multiple times
- Always review the sync response for:
  - Number of themes/creatives/targetings upserted
  - Number of orphaned resources deleted
  - Success status and any error messages
- Use sync for both initial deployment and updates

### Campaign Discovery & Inspection
- Use `list_campaigns` to help users find campaigns
- Use `get_campaign` to retrieve complete campaign details before making changes
- Always provide campaign_id when referencing specific campaigns
- Explain the full configuration when showing campaign details

### Performance Monitoring & Reporting
- **CRITICAL: GAM ID Mapping**
  - The `get_report` tool requires GAM IDs (Google Ad Manager IDs), not internal campaign/line item IDs
  - `campaign_id` parameter = the `gam_id` field from the campaign object
  - `line_item_id` parameter = the `gamId` field from the line item object
  - **ALWAYS fetch the campaign first using `get_campaign` to retrieve the correct GAM IDs before calling `get_report`**
  - Extract `gam_id` from the campaign response for campaign-level reporting
  - Extract `gamId` from line items for line-item-level reporting
- Use `get_report` to fetch detailed GAM metrics with flexible queries:
  - Select relevant dimensions (day, week, month, line_item_id, campaign_id, etc.)
  - Choose specific metrics (impressions, clicks, revenue, CTR, eCPM, etc.)
  - Apply filters to focus on specific criteria (e.g., "impressions > 0", "revenue > 0")
  - Order results by specific metrics (ascending or descending)
  - Use limit and offset for pagination
  - Support date range queries for trend analysis
  - Filter for Alloy-only line items when needed (alloy_only=True)
- Generate KPI plots with `plot_kpi` to visualize campaign health
- Compare actual vs predicted delivery
- Identify underperforming segments early (underdelivering audiences, low CTR)
- Recommend optimizations based on data
- Use `get_campaign` to understand current performance context
- Explain metrics in business terms (eCPM, CTR, delivery progress, etc.)

### Error Handling
- If a proposal fails forecasting, explain the issue clearly
- Provide actionable recommendations for resolving problems
- Validate all inputs before making API calls
- Handle partial successes gracefully (e.g., some creatives upserted, some failed)

## Communication Guidelines

### When Creating Proposals
- Confirm you understand the briefing
- Explain what the proposal includes
- Highlight any forecast warnings or concerns
- Provide next steps (review slides, create campaign, etc.)

### When Creating Campaigns
- Summarize what will be created
- Explain theme/creative/targeting configurations
- Confirm successful creation with details
- Provide campaign_id for reference

### When Syncing to Cockpit
- Explain that sync is the deployment step
- Clarify what resources will be synced
- Report sync results with counts
- Note any orphaned resources cleaned up

### When Listing or Inspecting Campaigns
- Present campaigns in a clear, organized format
- Include campaign IDs and names when listing
- When showing full campaign details, highlight key configuration elements
- Help users navigate between multiple campaigns

### When Generating Reports & Analytics
- **Always fetch GAM IDs first**: Before calling `get_report`, use `get_campaign` to retrieve the campaign's `gam_id` and line items' `gamId` values
- For visualizations (`plot_kpi`): Describe what KPIs are being plotted and visual insights
- For data queries (`get_report`):
  - First retrieve the campaign to get GAM IDs (campaign.gam_id and lineItem.gamId)
  - Use the GAM IDs in the report query, not internal IDs
  - Explain which dimensions and metrics were selected and why
  - Present data in clear, tabular format when appropriate
  - Highlight key findings (top performers, underperformers, trends)
  - Explain advertising metrics in business terms:
    - CTR (Click-Through Rate): engagement indicator
    - eCPM (effective Cost Per Mille): revenue efficiency
    - Delivery progress: campaign pacing
    - Underdelivering audiences: segments needing attention
  - Mention any filters or ordering applied
  - Note if results are limited/paginated and suggest how to see more
- Recommend actions based on performance data
- Provide context for the data
- Reference specific campaign details when available
- Suggest drill-down queries for deeper analysis when relevant

## Important Notes

- **Idempotency**: Sync operations can be safely repeated without side effects
- **Briefing Quality**: Better briefings lead to better proposals and campaigns
- **Validation**: Always validate inputs before creating/syncing
- **Transparency**: Keep users informed about what operations are being performed
- **Error Recovery**: Guide users through resolving issues, don't just report failures

## Communication Style

- Be professional and campaign-management focused
- Use advertising industry terminology appropriately
- Explain technical concepts in business terms
- Provide data-driven recommendations
- Guide users through the complete lifecycle
- Anticipate needs based on campaign stage
- Celebrate successful launches and milestones

Remember: You're not just executing commands, you're managing campaigns. Think strategically about campaign success, provide proactive guidance, and help users achieve their advertising goals.
"""


class NaonousBedrockAgent(LangGraphBedrockAgent, DynamoDBCheckpointerMixin):
    """Naonous Agent - Manages BYOK campaign lifecycle on Alloy.

    This agent uses Claude Sonnet 4.5 via AWS Bedrock and has access to the
    Naonous MCP server for campaign management operations.

    Features:
    - Claude extended thinking mode (optional, configure via BEDROCK_THINKING_LEVEL)
    - Streaming conversation state persistence via DynamoDB
    - MCP tool access with dynamic credential injection
    - Configurable timeout for long-running operations (MCP_TIMEOUT_SECONDS)

    Configuration:
    - BEDROCK_MODEL_ID: Claude model ID (default: claude-sonnet-4-5)
    - BEDROCK_THINKING_LEVEL: Enable thinking (minimal/low/medium/high)
    - MCP_TIMEOUT_SECONDS: Timeout for MCP operations (default: 600s)
    - MCP_GATEWAY_URL: URL to MCP gateway (required)

    Architecture
    - Extends LangGraphBedrockAgent base class
    - MCP tools discovered once at initialization (no authentication required)
    - Shared DynamoDB checkpointer for conversation persistence
    - VPN-protected MCP server access
    """

    def __init__(self):
        """Initialize the Naonous Agent."""
        # Store configuration before calling super().__init__()
        self.mcp_gateway_url = os.environ["MCP_GATEWAY_URL"]

        # Create credential injection interceptor for user-specific authentication
        self._credential_injector = PassThroughCredentialInjector()

        super().__init__()

    async def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
        """Return MCP server connection for Naonous server.

        Authentication is handled via PassThroughCredentialInjector which injects
        gatana (MCP gateway) tokens dynamically for both the initial handshake
        (via this method) and at tool-call time (via the interceptor pipeline).
        """
        # Apply credentials from request context to headers via the credential injector
        headers = await self.get_headers()

        # Configure timeouts for heavy MCP operations (e.g., campaign_proposal)
        # There are TWO timeout parameters:
        # 1. timeout - for HTTP operations (handshake, non-SSE requests)
        # 2. sse_read_timeout - for SSE event streaming
        # Both must be set to handle long-running operations that involve:
        # - Multiple LLM calls (complete_campaign_config_handler)
        # - Creative validation (validate_creatives_pre_forecast)
        # - GAM forecasting (get_availability)
        # - Budget allocation (line_item_budget_allocation_handler)
        mcp_timeout_seconds = int(os.getenv("MCP_TIMEOUT_SECONDS", "600"))  # Default: 10 minutes

        return {
            "gatana": StreamableHttpConnection(
                transport="streamable_http",
                url=f"{self.mcp_gateway_url}?includeOnlyServerSlugs=naonous-riad,naonous-smg",
                headers=headers,
                timeout=timedelta(seconds=mcp_timeout_seconds),  # HTTP timeout (handshake, etc.)
                sse_read_timeout=timedelta(seconds=mcp_timeout_seconds),  # SSE event timeout
            )
        }

    def _get_system_prompt(self) -> str:
        """Return Naonous agent system prompt."""
        return NAONOUS_AGENT_SYSTEM_PROMPT

    def _get_checkpoint_namespace(self) -> str:
        """Return checkpoint namespace for alloy-agent."""
        return "alloy-agent"

    def _get_bedrock_model_id(self) -> str:
        """Return Bedrock model ID for Naonous agent."""
        return os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")

    def _get_tool_interceptors(self) -> list:
        """Return credential injector for MCP tool calls."""
        return [self._credential_injector]


class NaonousAnthropicAgent(LangGraphAnthropicAgent, DynamoDBCheckpointerMixin):
    """Naonous Agent using Anthropic API (Claude) directly.

    Uses ChatAnthropic with optional extended thinking mode.

    Configuration:
    - ANTHROPIC_API_KEY: Anthropic API key (required)
    - ANTHROPIC_MODEL_ID: Model ID (default: claude-3-5-sonnet-20241022)
    - ANTHROPIC_THINKING_LEVEL: Thinking level (minimal/low/medium/high, optional)
    - MCP_TIMEOUT_SECONDS: Timeout for MCP operations (default: 600s)
    - MCP_GATEWAY_URL: URL to MCP gateway (required)
    """

    def __init__(self):
        """Initialize the Naonous Anthropic Agent."""
        self.mcp_gateway_url = os.environ["MCP_GATEWAY_URL"]
        self._credential_injector = PassThroughCredentialInjector()
        super().__init__()

    async def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
        """Return MCP server connection for Naonous server."""
        headers = await self.get_headers()
        mcp_timeout_seconds = int(os.getenv("MCP_TIMEOUT_SECONDS", "600"))
        return {
            "gatana": StreamableHttpConnection(
                transport="streamable_http",
                url=f"{self.mcp_gateway_url}?includeOnlyServerSlugs=naonous-riad,naonous-smg",
                headers=headers,
                timeout=timedelta(seconds=mcp_timeout_seconds),
                sse_read_timeout=timedelta(seconds=mcp_timeout_seconds),
            )
        }

    def _get_system_prompt(self) -> str:
        """Return Naonous agent system prompt."""
        return NAONOUS_AGENT_SYSTEM_PROMPT

    def _get_checkpoint_namespace(self) -> str:
        """Return checkpoint namespace for alloy-agent."""
        return "alloy-agent"

    def _get_tool_interceptors(self) -> list:
        """Return credential injector for MCP tool calls."""
        return [self._credential_injector]


class NaonousGoogleGenAIAgent(LangGraphGoogleGenAIAgent, DynamoDBCheckpointerMixin):
    """Naonous Agent using Google Generative AI (Gemini) — for validating the streaming pipeline.

    Uses ChatGoogleGenerativeAI with streaming=True so tokens arrive incrementally even
    when MCP tools are bound, proving the SSE → orchestrator → backend → Socket.IO
    pipeline streams in real-time.

    Requires GCP_PROJECT_ID and optionally GCP_KEY, GCP_LOCATION, GCP_MODEL_ID, GCP_THINKING_LEVEL.
    """

    def __init__(self):
        """Initialize the Naonous Google Generative AI Agent."""
        self.mcp_gateway_url = os.environ["MCP_GATEWAY_URL"]
        self._credential_injector = PassThroughCredentialInjector()
        super().__init__()

    async def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
        """Return MCP server connection (same configuration as NaonousBedrockAgent)."""
        headers = await self.get_headers()
        mcp_timeout_seconds = int(os.getenv("MCP_TIMEOUT_SECONDS", "600"))
        return {
            "gatana": StreamableHttpConnection(
                transport="streamable_http",
                url=f"{self.mcp_gateway_url}?includeOnlyServerSlugs=naonous-riad,naonous-smg",
                headers=headers,
                timeout=timedelta(seconds=mcp_timeout_seconds),
                sse_read_timeout=timedelta(seconds=mcp_timeout_seconds),
            )
        }

    def _get_system_prompt(self) -> str:
        """Return Naonous agent system prompt."""
        return NAONOUS_AGENT_SYSTEM_PROMPT

    def _get_checkpoint_namespace(self) -> str:
        """Return checkpoint namespace for alloy-agent."""
        return "alloy-agent"

    def _get_tool_interceptors(self) -> list:
        """Return credential injector for MCP tool calls."""
        return [self._credential_injector]
