# Nanous Agent Copilot Instructions

## Tech Stack

- FastAPI with async/await
- LangGraph for agent orchestration
- SQLAlchemy not used (no database - campaign data managed via MCP tools)
- DynamoDB for checkpoints
- S3 for checkpoint storage
- Pydantic v2 for data validation
- pytest with pytest-asyncio for testing
- AWS Bedrock (Claude Sonnet 4.5) for LLM capabilities

## Local Development Environment

**CRITICAL: Any changes that impact the local development environment MUST be reflected in `/start-dev.sh`**

This includes:
- New environment variables (add to SSM fetching or default values in start-dev.sh)
- New secrets/credentials (add AWS SSM parameter fetching if needed)
- Configuration changes that affect local setup
- New service dependencies or startup requirements
- Changes to `.env` or `.env.template` files

The `start-dev.sh` script is the single source of truth for local environment setup. Always update it when making changes that affect how the application runs locally.

## Code Style

- Use async/await for all I/O operations
- Type hints are required for all function signatures
- Use dependency injection via FastAPI's `Depends()`
- Prefer explicit over implicit error handling

## Architecture Patterns

### LangGraph Agents

- All agent logic is implemented using LangGraph
- Use `StateGraph` for defining agent workflows
- Implement proper state management with typed state classes
- Use checkpointers for agent persistence (DynamoDB + S3)

### MCP Tool Integration

- MCP tools are discovered from the Nanous MCP server at `https://naonous.d.alloy.rcplus.io/mcp`
- No authentication required (VPN-protected access)
- Tools are discovered once at initialization and reused
- Use `StreamableHttpConnection` for MCP server connection
- Tools are loaded via `langchain_mcp_adapters`
- TenantEnforcementMiddleware enforces 'riad' tenant is used for all the tool calls

### Campaign Management

The agent manages the complete campaign lifecycle:

1. **Proposal Phase**: Create and refine campaign proposals
2. **Creation Phase**: Convert proposals to campaigns
3. **Deployment Phase**: Sync campaigns to Cockpit
4. **Monitoring Phase**: Track KPIs and performance
5. **Update Phase**: Modify and re-sync campaigns

### Configuration

- All configuration is loaded from environment variables via direct `os.getenv()`
- No Pydantic Settings model needed (simpler approach)
- Support multiple environments (local, dev, stg, prod)
- VPN-protected MCP server access (no auth tokens required)

## Testing

- Use pytest with pytest-asyncio for async tests
- Use aiomoto for mocking AWS services
- Mock external dependencies (Bedrock, MCP server, etc.)
- Test LangGraph workflows with mock checkpointers
- Verify campaign management flows with test scenarios

## Available MCP Tools

The agent has access to these campaign management tools from the Nanous MCP server:

### Campaign Proposal Tools
- `campaign_proposal_proposal_campaign_create_post` - Create proposals
- `campaign_proposal_search_proposal_campaign_search_post` - Search proposals
- `campaign_proposal_slides_create_proposal_slides_create_post` - Generate slides
- `campaign_proposal_slides_status_proposal_slides_status_post` - Check slide status

### Campaign Creation Tools
- `create_from_proposal` - Create campaign from proposal object
- `create_from_proposal_id` - Create campaign from proposal ID

### Campaign Deployment Tools
- `sync_campaign_to_cockpit` - Sync complete campaign (idempotent)

### Campaign Analytics Tools
- `plot_kpi` - Generate KPI visualization plots

## Important Notes

- No authentication middleware required (VPN-protected)
- MCP server is accessible without credentials
- All campaign data is managed via MCP tools (no local database)
- Conversation state is persisted in DynamoDB checkpoints
- Claude Sonnet 4.5 provides campaign management expertise
- System prompt guides the agent through campaign lifecycle stages
