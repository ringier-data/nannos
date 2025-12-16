# Alloy Agent

A2A-compatible agent for managing BYOK (Bring Your Own KPI) campaign lifecycle on Alloy.

## Overview

The Alloy Agent is a specialized AI agent that helps users manage the complete lifecycle of BYOK advertising campaigns through natural language conversation. It provides comprehensive campaign management capabilities including:

- **Campaign Proposal**: Create and manage campaign proposals from briefings
- **Campaign Creation**: Convert proposals into active campaigns
- **Campaign Deployment**: Sync campaigns to Cockpit for execution
- **Campaign Monitoring**: Track performance with KPI visualizations
- **Campaign Updates**: Modify and re-sync campaign configurations

## Features

- **Natural Language Interface**: Interact with campaigns using conversational queries
- **Complete Lifecycle Management**: From proposal to deployment to monitoring
- **MCP Tool Integration**: Access to Nanous MCP server tools
- **Claude Sonnet 4.5**: Powered by AWS Bedrock for intelligent responses
- **Conversation Persistence**: DynamoDB checkpointer maintains conversation context
- **VPN-Protected**: No authentication required (behind VPN)

## Architecture

- **LangGraph**: Agent orchestration and workflow management
- **AWS Bedrock**: Claude Sonnet 4.5 for natural language understanding
- **MCP Protocol**: Tool discovery and execution via Nanous MCP server
- **DynamoDB**: Conversation checkpointing and state persistence
- **FastAPI**: A2A-compatible server implementation

## Configuration

Create a `.env` file from `.env.template`:

```bash
cp .env.template .env
```

Key configuration options:

- `NAONOUS_MCP_URL`: URL to the Nanous MCP server (default: https://naonous.d.alloy.rcplus.io/mcp)
- `BEDROCK_MODEL_ID`: AWS Bedrock model ID (default: Claude Sonnet 4.5)
- `CHECKPOINT_DYNAMODB_TABLE_NAME`: DynamoDB table for checkpoints
- `PORT`: Server port (default: 5004)

## Installation

### Local Development

```bash
# Install dependencies
make install

# Or with development tools
make dev
```

### Docker

```bash
# Build image
make docker-build

# Run container
make docker-run
```

## Running the Agent

### Local

```bash
# Using make
make run

# Or directly
python main.py
```

The server will start on `http://localhost:5004` (or configured PORT).

### Using start-dev.sh

From the repository root:

```bash
# Run all services locally (including naonous-agent)
./start-dev.sh

# Run naonous-agent on dev environment
./start-dev.sh --naonous dev

# Follow logs
tail -f logs/naonous.log
```

## API Endpoints

### Health Check
```bash
GET /health
```

### A2A Endpoints
The agent implements the A2A protocol via the Ringier A2A SDK middleware:
- `POST /` - Execute agent tasks
- `GET /capabilities` - Get agent capabilities

## Development

### Run Tests
```bash
make test
```

### Linting and Formatting
```bash
# Check code
make lint

# Format code
make format
```

### Clean Build Artifacts
```bash
make clean
```

## Usage Examples

### Creating a Campaign Proposal

```
User: "Create a campaign proposal for promoting our new product line. 
       Target audience is 25-40 year olds in urban areas. Budget is $50k."

Agent: [Gathers details, creates proposal, reports allocation and forecast]
```

### Deploying a Campaign

```
User: "Sync campaign ID 12345 to Cockpit"

Agent: [Syncs campaign, reports upserted themes/creatives/targetings, 
        confirms successful deployment]
```

### Monitoring Performance

```
User: "Show me KPI plots for campaign 12345"

Agent: [Generates visualization, provides insights and recommendations]
```

## Integration with Alloy Infrastructure

The Nanous Agent is part of the Alloy Infrastructure Agents ecosystem:

- Communicates with other agents via A2A protocol
- Discoverable by the orchestrator agent
- Can be invoked for campaign-specific tasks
- Shares authentication and configuration patterns

## Deployment

The agent can be deployed using Ansible playbooks (infrastructure as code):

```bash
cd infrastructure
ansible-playbook playbook.yml --tags naonous-agent
```

See `/infrastructure/roles/naonous-agent/` for deployment configuration.

## Troubleshooting

### Connection Issues
- Verify VPN connection (MCP server is VPN-protected)
- Check `NAONOUS_MCP_URL` configuration
- Ensure network access to `naonous.d.alloy.rcplus.io`

### AWS Credentials
- Configure AWS CLI: `aws configure`
- Or use IAM role in production
- Verify Bedrock access in the configured region

### DynamoDB Checkpointing
- Ensure table exists: `CHECKPOINT_DYNAMODB_TABLE_NAME`
- Verify IAM permissions for DynamoDB access
- Check AWS region configuration

### Tool Discovery
- MCP tools are discovered on first request
- Check logs for discovery errors
- Verify MCP server is accessible

## Contributing

Follow the established patterns in the codebase:
- Type hints for all functions
- Async/await for I/O operations
- Comprehensive docstrings
- Unit tests for new functionality

## License

See repository LICENSE file.
