"""Naonous Agent - Manages BYOK campaign lifecycle on Alloy.

This module implements an A2A agent that helps users manage the complete
lifecycle of BYOK (Bring Your Own KPI) campaigns through natural language conversation.
"""

import logging
import os
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any

import boto3
from a2a.types import Task, TaskState
from botocore.config import Config as BotoConfig
from deepagents import create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import AutoStrategy
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langgraph.graph.state import CompiledStateGraph
from langgraph_checkpoint_aws import DynamoDBSaver
from pydantic import BaseModel, Field
from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig

logger = logging.getLogger(__name__)


# Default tenant for all MCP tool calls
DEFAULT_TENANT = "riad"


class TenantEnforcementMiddleware(AgentMiddleware[dict[str, Any], None]):
    """AgentMiddleware that enforces tenant parameter in all tool calls.

    This middleware intercepts tool calls at execution time and automatically
    overrides the 'tenant' parameter to DEFAULT_TENANT for any tool that has
    a tenant parameter in its arguments.

    The enforcement happens at the tool call level (awrap_tool_call) by
    modifying the tool call arguments before the tool is executed.
    """

    tools: list[BaseTool] = []  # No tools registered with middleware itself

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Intercept tool call and enforce tenant parameter.

        This async wrap-style hook:
        1. Checks if the tool call has a 'tenant' argument
        2. If present, overrides it with DEFAULT_TENANT
        3. Executes the tool via handler with modified arguments
        4. Returns the tool result

        Args:
            request: Tool call request with tool name and arguments
            handler: Async callback to execute the tool

        Returns:
            ToolMessage or Command from tool execution
        """
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {})

        # Check if this tool call has a tenant parameter
        if isinstance(args, dict) and "tenant" in args:
            original_tenant = args.get("tenant")
            args["tenant"] = DEFAULT_TENANT
            logger.debug(f"Enforcing tenant={DEFAULT_TENANT} for tool '{tool_name}' (original: {original_tenant})")

        # Execute the tool with modified arguments
        result = await handler(request)
        return result


NAONOUS_AGENT_SYSTEM_PROMPT = """You are an expert Campaign Manager for Alloy's BYOK (Bring Your Own KPI) platform. Your role is to manage the complete lifecycle of advertising campaigns through natural language conversation.

## Your Capabilities

You have access to comprehensive campaign management tools via the Naonous MCP server:

### Campaign Proposal Tools
1. **campaign_proposal** - Create new campaign proposals from briefings
2. **campaign_proposal_search** - Search existing campaign proposals
3. **campaign_proposal_slides_create** - Generate presentation slides for proposals
4. **campaign_proposal_slides_status** - Check status of slide generation jobs

### Campaign Management
5. **list_campaigns** - List all campaigns with basic information (id, gam_id, name, start_date, end_date) for a tenant
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
1. **List Campaigns**: Use `list_campaigns` to see all available campaigns for a tenant
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
- Ensure all required fields are provided (tenant, briefing, etc.)
- Validate budget allocations across line items
- Confirm creative formats match targeting requirements
- Review regional settings for proper localization

### Syncing to Cockpit
- The sync operation is idempotent - safe to call multiple times
- Always review the sync response for:
  - Number of themes/creatives/targetings upserted
  - Number of orphaned resources deleted
  - Success status and any error messages
- Use sync for both initial deployment and updates

### Campaign Discovery & Inspection
- Use `list_campaigns` to help users find campaigns by tenant
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


class NaonousAgent(BaseAgent):
    """Naonous Agent - Manages BYOK campaign lifecycle on Alloy.

    This agent uses Claude Sonnet 4.5 via AWS Bedrock and has access to the
    Naonous MCP server for campaign management operations.

    Architecture:
    - MCP tools discovered once at initialization (no authentication required)
    - Shared DynamoDB checkpointer for conversation persistence
    - Single graph instance reused across requests
    - VPN-protected MCP server access
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self):
        """Initialize the Naonous Agent.

        Discovers MCP tools from Naonous server and creates the DeepAgent graph.
        """
        super().__init__()

        # Configuration from environment
        self.naonous_mcp_url = os.getenv("NAONOUS_MCP_URL", "https://naonous.d.alloy.rcplus.io/mcp")
        self.bedrock_region = os.getenv("AWS_BEDROCK_REGION", "eu-central-1")
        self.bedrock_model_id = os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")

        # Checkpointer configuration
        checkpoint_table = os.getenv(
            "CHECKPOINT_DYNAMODB_TABLE_NAME", "dev-alloy-infrastructure-agents-langgraph-checkpoints"
        )
        checkpoint_region = os.getenv("CHECKPOINT_AWS_REGION", "eu-central-1")
        checkpoint_ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
        checkpoint_compression = os.getenv("CHECKPOINT_COMPRESSION_ENABLED", "true").lower() == "true"
        checkpoint_s3_bucket = os.getenv(
            "CHECKPOINT_S3_BUCKET_NAME", "dev-alloy-infrastructure-agents-orchestrator-checkpoints"
        )

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

        # MCP tools will be discovered lazily on first request
        self._mcp_tools: list[BaseTool] | None = None
        self._mcp_tools_lock = False
        logger.info("MCP tool discovery will happen on first request")

        # Configure boto3 client with timeouts and retry logic
        read_timeout = int(os.getenv("BEDROCK_READ_TIMEOUT", "300"))
        connect_timeout = int(os.getenv("BEDROCK_CONNECT_TIMEOUT", "10"))
        max_attempts = int(os.getenv("BEDROCK_MAX_RETRY_ATTEMPTS", "3"))
        retry_mode = os.getenv("BEDROCK_RETRY_MODE", "adaptive")

        boto_config = BotoConfig(
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
            retries={
                "max_attempts": max_attempts,
                "mode": retry_mode,
            },
        )

        # Create bedrock-runtime client
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
        self._mcp_client: MultiServerMCPClient | None = None

    async def _ensure_mcp_tools_loaded(self):
        """Ensure MCP tools are discovered and loaded.

        This is called lazily on first request to avoid blocking __init__.
        Creates a fallback graph without MCP tools if discovery fails.
        """
        if self._mcp_tools is not None and self._graph is not None:
            return

        if self._mcp_tools_lock:
            import asyncio

            for _ in range(10):
                await asyncio.sleep(0.1)
                if self._mcp_tools is not None and self._graph is not None:
                    return
            logger.warning("Timeout waiting for MCP tools discovery")
            # Continue to create fallback graph

        self._mcp_tools_lock = True
        try:
            logger.info("Discovering MCP tools from Naonous server...")

            # Create MCP client (no authentication needed - behind VPN)
            connections = {
                "naonous": StreamableHttpConnection(
                    transport="streamable_http",
                    url=self.naonous_mcp_url,
                ),
            }

            self._mcp_client = MultiServerMCPClient(connections=connections)

            # Load tools without authentication
            from langchain_mcp_adapters.tools import load_mcp_tools

            self._mcp_tools = await load_mcp_tools(
                session=None,
                connection=connections["naonous"],
                server_name="naonous",
            )

            logger.info(f"Discovered {len(self._mcp_tools)} MCP tools")

            # Create graph with MCP tools and tenant enforcement middleware
            logger.info("Creating graph with MCP tools and tenant enforcement middleware...")
            tools = self._mcp_tools

            self._graph = create_deep_agent(
                model=self._model,
                tools=tools,
                subagents=[],
                system_prompt=NAONOUS_AGENT_SYSTEM_PROMPT,
                checkpointer=self._checkpointer,
                middleware=[TenantEnforcementMiddleware()],
                response_format=AutoStrategy(schema=FinalResponseSchema),
            )
            logger.info("Graph created with MCP tools")
        except Exception as e:
            logger.error(f"Failed to discover MCP tools: {e}", exc_info=True)
            logger.warning("Creating fallback graph without MCP tools")

            # Create fallback graph without MCP tools
            self._mcp_tools = []
            try:
                self._graph = create_deep_agent(
                    model=self._model,
                    tools=[],
                    subagents=[],
                    system_prompt=(
                        "You are an expert Campaign Manager for Alloy's BYOK platform. "
                        "However, you are currently unable to access campaign management tools "
                        "due to a connection issue. Please inform the user that the system is "
                        "temporarily unavailable and suggest they try again later or contact support."
                    ),
                    checkpointer=self._checkpointer,
                    middleware=[],
                    response_format=AutoStrategy(schema=FinalResponseSchema),
                )
                logger.info("Fallback graph created successfully")
            except Exception as fallback_error:
                logger.error(f"Failed to create fallback graph: {fallback_error}", exc_info=True)
                raise RuntimeError(
                    "Unable to initialize agent: both MCP tool loading and fallback graph creation failed"
                ) from fallback_error
        finally:
            self._mcp_tools_lock = False

    async def close(self):
        """Cleanup resources."""
        logger.info("NaonousAgent closed")

    async def stream(self, query: str, user_config: UserConfig, task: Task) -> AsyncIterable[AgentStreamResponse]:
        """Stream responses for a user query.

        Args:
            query: The user's natural language query
            user_config: User configuration (not used - no auth required)
            task: The task context for the current interaction

        Yields:
            AgentStreamResponse objects with state updates and content
        """
        try:
            # Ensure MCP tools are loaded
            await self._ensure_mcp_tools_loaded()

            # Verify graph was created
            if self._graph is None:
                logger.error("Graph is None after _ensure_mcp_tools_loaded()")
                yield AgentStreamResponse(
                    state=TaskState.failed,
                    content="The agent failed to initialize properly. Please contact support or try again later.",
                    metadata={"error": "graph_initialization_failed"},
                )
                return

            logger.info(f"Processing query for user {user_config.user_id}")

            # Execute graph
            config = {
                "configurable": {
                    "thread_id": task.context_id,
                }
            }

            # Convert query to messages format
            from langchain_core.messages import AIMessage, HumanMessage

            input_messages = [HumanMessage(content=query)]

            # Stream graph execution
            chunk_count = 0
            final_user_content = []

            async for event in self._graph.astream({"messages": input_messages}, config):
                chunk_count += 1
                logger.debug(f"Graph event #{chunk_count}: {type(event)}")

                if isinstance(event, dict):
                    for node_name, node_data in event.items():
                        if isinstance(node_data, dict) and "messages" in node_data:
                            messages = node_data["messages"]
                            if isinstance(messages, list):
                                for msg in messages:
                                    if isinstance(msg, AIMessage) and msg.content:
                                        content = str(msg.content)
                                        logger.debug(f"Content from {node_name}: {content[:100]}...")
                                        final_user_content.append(content)
                                        yield AgentStreamResponse(
                                            state=TaskState.working,
                                            content=content,
                                        )

            logger.debug(f"Stream processing complete. Total chunks: {chunk_count}")

            # Get final state
            final_state = self._graph.get_state(config)

            # Check for interrupts
            if final_state.interrupts:
                yield AgentStreamResponse(
                    state=TaskState.input_required,
                    content="Process interrupted. Additional input required.",
                )
                return

            # Extract final state from FinalResponseSchema
            task_state = TaskState.completed

            if final_state.values and "messages" in final_state.values:
                messages = final_state.values["messages"]
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage):
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            for tool_call in msg.tool_calls:
                                if tool_call.get("name") == "FinalResponseSchema":
                                    args = tool_call.get("args", {})
                                    state_str = args.get("task_state", "completed")
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

            # Send final completion
            final_content = "\n\n".join(final_user_content) if final_user_content else "Request processed successfully."

            logger.info(f"Sending final completion: task_state={task_state}, content_length={len(final_content)}")
            yield AgentStreamResponse(
                state=task_state,
                content=final_content,
            )
            logger.info("Final response sent successfully")

        except Exception as e:
            logger.error(f"Error in NaonousAgent.stream: {e}", exc_info=True)
            yield AgentStreamResponse(
                state=TaskState.failed,
                content=f"An error occurred while processing your request: {str(e)}",
                metadata={"error": str(e)},
            )
