# Orchestrator Agent

An intelligent multi-agent orchestrator built on the Agent-to-Agent (A2A) protocol that plans and coordinates complex tasks by discovering and delegating to specialized sub-agents.

## Overview

The Orchestrator Agent is an enterprise-grade agentic system that:

- **Plans Complex Tasks**: Breaks down user requests into manageable subtasks
- **Discovers Sub-Agents**: Dynamically discovers available specialized agents (currency converter, JIRA integration, etc.)
- **Coordinates Execution**: Delegates work to appropriate sub-agents and aggregates results
- **Provides Status Updates**: Real-time task status and pallorogress reporting
- **Supports Authentication**: Okta OAuth2 integration for enterprise security

## Architecture

### Core Components

```
┌─────────────────────────────────────────────────────────────┐
│                    User Request                             │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              A2A Server (Starlette)                         │
│  • Okta Auth Middleware                                     │
│  • User Context Middleware                                  │
│  • Request Handler                                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│         OrchestratorDeepAgent (LangGraph)                   │
│  • LLM Planning (GPT-4o/Gemini)                            │
│  • Todo Management                                          │
│  • Sub-Agent Discovery                                      │
│  • Task Delegation                                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            Sub-Agents (A2A Protocol)                        │
│  • Currency Converter Agent                                 │
│  • JIRA Integration Agent                                   │
│  • ... (dynamically discovered)                             │
└─────────────────────────────────────────────────────────────┘
```

### Key Technologies

- **A2A SDK**: Agent-to-Agent protocol implementation
- **LangGraph**: State machine for agent workflows
- **LangChain**: LLM orchestration (OpenAI/Azure/Google)
- **DynamoDB**: Checkpoint persistence for conversation state
- **Okta**: OAuth2 authentication and authorization
- **Starlette**: ASGI web framework
- **SSE**: Server-Sent Events for streaming responses

## Features

### Multi-Turn Conversations
- Maintains conversation context across multiple interactions
- Persists state in DynamoDB for session continuity
- Supports thread-based conversations with unique context IDs

### Dynamic Agent Discovery
- Automatically discovers available sub-agents from agent registry
- Fetches agent capabilities and available tools
- Adapts tool usage based on discovered agent features

### Human-in-the-Loop
- Requests clarification when user intent is ambiguous
- Supports interactive task refinement
- Handles user confirmations and approvals

### Status Updates
- Real-time task progress reporting via A2A status-update messages
- Todo list tracking with not-started/in-progress/completed states
- Streaming responses for immediate user feedback

### Security
- OAuth2 authentication via Okta
- JWT token validation
- Public agent card endpoint (no auth required)
- User context propagation to sub-agents

## Quick Start

### Prerequisites

- Python 3.13+
- Okta account and application credentials
- OpenAI/Azure OpenAI/Google API key
- AWS credentials (for DynamoDB checkpoints)

### Installation

```bash
# Clone repository
cd app/orchestrator-agent

# Install dependencies (using uv or pip)
uv sync
# or
pip install -e .

# Copy environment template
cp .env.template .env
```

### Configuration

Edit `.env` with your credentials:

```bash
# LLM Configuration
model_source=azure  # or "openai" or "google"
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o
AZURE_OPENAI_API_VERSION=2024-08-01-preview

# Okta Authentication
OKTA_DOMAIN=rcplus.okta.com
OKTA_CLIENT_ID=your-client-id
OKTA_AUDIENCE=api://default

# DynamoDB Checkpoints (for conversation persistence)
AWS_DEFAULT_REGION=us-east-1
DYNAMODB_TABLE_NAME=orchestrator-checkpoints

# Agent Discovery
AGENT_REGISTRY_URL=http://localhost:9000/agents

# Logging
LOG_LEVEL=INFO
LOG_MODE=JSON
```

### Running the Server

```bash
# Start the server
python main.py --host localhost --port 10001

# Or using Make
make run
```

The server will start on `http://localhost:10001`

### Verify Installation

```bash
# Check agent card (no auth required)
curl http://localhost:10001/.well-known/agent-card.json | jq .

# Test with authentication (requires Okta token)
curl -X POST http://localhost:10001/message/send \
  -H "Authorization: Bearer YOUR_OKTA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "parts": [{"text": "Convert 100 USD to EUR"}]
    }
  }'
```

## Authentication

### Obtaining an Okta Token

1. Navigate to Okta OAuth2 authorization endpoint:
```
https://rcplus.okta.com/oauth2/v1/authorize?client_id=YOUR_CLIENT_ID&response_type=code&scope=openid%20profile%20email&redirect_uri=YOUR_REDIRECT_URI&state=random_state
```

2. Exchange authorization code for token:
```bash
curl -X POST https://rcplus.okta.com/oauth2/v1/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=YOUR_AUTH_CODE&redirect_uri=YOUR_REDIRECT_URI&client_id=YOUR_CLIENT_ID&client_secret=YOUR_SECRET"
```

3. Use the `access_token` in requests:
```bash
curl -H "Authorization: Bearer ACCESS_TOKEN" http://localhost:10001/message/send
```

### Public Endpoints

- `GET /.well-known/agent-card.json` - Agent card (no auth)
- `GET /health` - Health check (no auth)

### Protected Endpoints

- `POST /message/send` - Send message to agent
- `POST /context/create` - Create new conversation context
- `GET /context/{contextId}/messages` - Get conversation history
- `POST /task/cancel` - Cancel running task

## Testing

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=app --cov-report=html

# Run specific test files
pytest tests/domain/test_agent.py -v
pytest tests/integration/test_auth.py -v
```

### Manual Testing

```bash
# Multi-turn conversation test
./test_multiturn.sh

# Send test request
./send_test_request.sh
```

## Development

### Project Structure

```
orchestrator-agent/
├── app/
│   ├── agent.py                    # Main orchestrator agent
│   ├── agent_executor.py           # A2A executor wrapper
│   ├── a2a_factory.py              # A2A client factory
│   ├── discovery.py                # Sub-agent discovery
│   ├── okta_auth_middleware.py     # OAuth2 authentication
│   ├── todo_status_middleware.py   # Status update handling
│   ├── graph_manager.py            # LangGraph state management
│   └── ...
├── tests/
│   ├── domain/                     # Domain logic tests
│   ├── integration/                # Integration tests
│   └── ...
├── main.py                         # Server entry point
├── pyproject.toml                  # Dependencies
├── Dockerfile                      # Container definition
└── Makefile                        # Build/run commands
```

### Key Classes

- `OrchestratorDeepAgent`: Main agent implementing planning and delegation logic
- `OrchestratorDeepAgentExecutor`: A2A protocol wrapper for the agent
- `GraphManager`: Manages LangGraph state machine and checkpointing
- `OktaAuthMiddleware`: Validates JWT tokens and extracts user context
- `TodoStatusMiddleware`: Intercepts todo updates and emits status messages

### Adding New Tools

1. Define tool in `app/agent.py`:
```python
@tool
def my_custom_tool(arg: str) -> str:
    """Tool description for LLM."""
    return f"Result: {arg}"
```

2. Add to agent's tool list in `create_graph()` method

3. Update tests in `tests/domain/`

## Deployment

### Docker

```bash
# Build image
make docker-build

# Run container
docker run -p 10001:10001 --env-file .env orchestrator-agent
```

### Environment Variables

See `.env.template` for complete list of configuration options.

Key variables:
- `model_source`: LLM provider (azure/openai/google)
- `OKTA_*`: Okta authentication settings
- `DYNAMODB_TABLE_NAME`: Conversation checkpoint storage
- `AGENT_REGISTRY_URL`: Sub-agent discovery endpoint
- `LOG_LEVEL`: Logging verbosity (DEBUG/INFO/WARNING/ERROR)

## Troubleshooting

### Common Issues

**Authentication failures (401)**
- Verify Okta credentials in `.env`
- Check token expiration
- Ensure `OKTA_AUDIENCE` matches token audience

**Agent discovery failures**
- Verify `AGENT_REGISTRY_URL` is reachable
- Check sub-agent availability
- Review discovery logs with `LOG_LEVEL=DEBUG`

**DynamoDB checkpoint errors**
- Verify AWS credentials
- Ensure table exists: `aws dynamodb describe-table --table-name orchestrator-checkpoints`
- Check IAM permissions for DynamoDB access

**LLM API errors**
- Verify API key and endpoint configuration
- Check rate limits and quotas
- Review model deployment name

### Logs

Logs are written to:
- Console (stdout) - controlled by `LOG_LEVEL`
- `app.log` - application logs
- `server.log` - server access logs

View logs:
```bash
tail -f app.log
tail -f server.log
```

## Contributing

1. Create feature branch from `main`
2. Make changes with tests
3. Run tests: `pytest tests/ -v`
4. Submit pull request

## License

See LICENSE file in repository root.

## Support

For issues or questions, contact the Alloy team or file an issue in the repository.
