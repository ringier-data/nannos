# Orchestrator Agent Copilot Instructions

## Tech Stack

- FastAPI with async/await
- LangGraph for agent orchestration
- SQLAlchemy 2.0+ (async) with PostgreSQL and pgvector
- DynamoDB for checkpoints
- S3 for checkpoint storage
- Pydantic v2 for data validation
- pytest with pytest-asyncio for testing

## Local Development Environment

**CRITICAL: Any changes that impact the local development environment MUST be reflected in `/start-dev.sh`**

This includes:
- New environment variables (add to SSM fetching or default values in start-dev.sh)
- New secrets/credentials (add AWS SSM parameter fetching)
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

### Document Store

- PostgreSQL with pgvector for document storage and semantic search
- Use async SQLAlchemy operations for all database interactions
- Implement proper connection pooling and session management
- Document embeddings are stored in vector columns for similarity search

### Configuration

- All configuration is loaded from environment variables via Pydantic models
- Use `SecretStr` for sensitive values
- Validate configuration at startup
- Support multiple environments (local, dev, stg, prod)

## Distributed Tracing

### A2A Sub-Agent Tracing

The orchestrator propagates LangSmith trace context to all A2A sub-agents (agent-creator, alloy-agent) for unified distributed tracing.

**Implementation in `app/subagents/runnable.py`:**

- `A2AClientRunnable._inject_trace_headers()`: Async event hook that dynamically injects trace headers for each HTTP request
- Uses `get_current_run_tree().to_headers()` to get trace context at request time
- Registered via httpx `event_hooks={"request": [self._inject_trace_headers]}`

**CRITICAL**: Do NOT inject trace headers at client initialization time. Headers must be injected dynamically for each request using event hooks, otherwise trace context will be stale or missing.

**How it works:**
1. When orchestrator invokes A2A sub-agent, the event hook captures the current LangSmith run context
2. Trace headers (`langsmith-trace`, `baggage`) are injected into the HTTP request
3. Sub-agent's `TracingMiddleware` receives headers and continues the trace
4. All operations appear as a single hierarchical trace in LangSmith

**Debugging:**
- Check logs for "Injected LangSmith trace headers" messages
- Verify `LANGSMITH_API_KEY` is configured for all services
- Ensure sub-agents have `TracingMiddleware` registered

## Testing

- Use pytest with pytest-asyncio for async tests
- Use aiomoto for mocking AWS services
- Mock external dependencies (OpenAI, etc.)
- Test LangGraph workflows with mock checkpointers
- Verify document store operations with test database
