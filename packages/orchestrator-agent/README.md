# Orchestrator Agent

An intelligent multi-agent orchestrator built on the Agent-to-Agent (A2A) protocol that plans and coordinates complex tasks by discovering and delegating to specialized sub-agents.

## Overview

The Orchestrator Agent is an enterprise-grade agentic system that:

- **Plans Complex Tasks**: Breaks down user requests into manageable subtasks
- **Discovers Sub-Agents**: Dynamically discovers available specialized agents (currency converter, JIRA integration, etc.)
- **Coordinates Execution**: Delegates work to appropriate sub-agents and aggregates results
- **Provides Status Updates**: Real-time task status and pallorogress reporting
- **Supports Authentication**: Oidc OAuth2 integration for enterprise security

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
│  • Oidc Auth Middleware                                     │
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
- **PostgreSQL**: Checkpoint persistence for conversation state (with optional S3 offload)
- **Oidc**: OAuth2 authentication and authorization
- **Starlette**: ASGI web framework
- **SSE**: Server-Sent Events for streaming responses

## Features

### Multi-Turn Conversations
- Maintains conversation context across multiple interactions
- Persists state in PostgreSQL for session continuity
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
- OAuth2 authentication via Oidc
- JWT token validation
- Public agent card endpoint (no auth required)
- User context propagation to sub-agents

## Quick Start

### Prerequisites

- Python 3.13+
- Oidc account and application credentials
- OpenAI/Azure OpenAI/Google API key
- PostgreSQL credentials (for checkpoint persistence)

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
# LLM Configuration — all LLM traffic routes through the Model Gateway (LiteLLM proxy);
# provider credentials and model routing live on the gateway, not here.
LLM_GATEWAY_URL=http://litellm-proxy.nannos.svc
# LLM_GATEWAY_API_KEY=sk-...   # proxy virtual/master key (from secret in prod)

# OIDC Authentication
OIDC_DOMAIN=rcplus.oidc.com
OIDC_CLIENT_ID=your-client-id
OIDC_AUDIENCE=api://default

# PostgreSQL Checkpoints (for conversation persistence)
# The checkpointer reuses the POSTGRES_* connection above (same DB/user as the document
# store). Checkpoint tables are created in POSTGRES_SCHEMA via the connection search_path
# by AsyncPostgresSaver.setup() — no separate database, user, or migration required.
CHECKPOINT_TTL_DAYS=14            # retention hint (not auto-enforced; see mixin docstring)

# Optional S3 Offloading (for checkpoint blobs above the threshold; default 1 MB)
# CHECKPOINT_S3_BUCKET_NAME=my-bucket
# CHECKPOINT_S3_THRESHOLD_MB=1

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

# Test with authentication (requires Oidc token)
curl -X POST http://localhost:10001/message/send \
  -H "Authorization: Bearer YOUR_OIDC_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "parts": [{"text": "Convert 100 USD to EUR"}]
    }
  }'
```

## Authentication

### Obtaining an Oidc Token

1. Navigate to Oidc OAuth2 authorization endpoint:
```
https://rcplus.oidc.com/oauth2/v1/authorize?client_id=YOUR_CLIENT_ID&response_type=code&scope=openid%20profile%20email&redirect_uri=YOUR_REDIRECT_URI&state=random_state
```

2. Exchange authorization code for token:
```bash
curl -X POST https://rcplus.oidc.com/oauth2/v1/token \
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
│   ├── oidc_auth_middleware.py     # OAuth2 authentication
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
- `OidcAuthMiddleware`: Validates JWT tokens and extracts user context
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
- `LLM_GATEWAY_URL`: Model Gateway (LiteLLM proxy) endpoint — all LLM traffic routes here
- `OIDC_*`: Oidc authentication settings
- `AGENT_REGISTRY_URL`: Sub-agent discovery endpoint
- `LOG_LEVEL`: Logging verbosity (DEBUG/INFO/WARNING/ERROR)

## Troubleshooting

### Common Issues

**Authentication failures (401)**
- Verify Oidc credentials in `.env`
- Check token expiration
- Ensure `OIDC_AUDIENCE` matches token audience

**Agent discovery failures**
- Verify `AGENT_REGISTRY_URL` is reachable
- Check sub-agent availability
- Review discovery logs with `LOG_LEVEL=DEBUG`

**PostgreSQL checkpoint errors**
- Verify PostgreSQL is running: `pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT"` (the checkpointer shares the docstore DB)
- Check checkpoint tables exist (in the POSTGRES_SCHEMA of the docstore DB): `psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt $POSTGRES_SCHEMA.checkpoint*"`
- Check IAM/credentials for checkpoint database
- For S3 offloading issues, verify S3 bucket permissions and `CHECKPOINT_S3_BUCKET_NAME` is set

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
