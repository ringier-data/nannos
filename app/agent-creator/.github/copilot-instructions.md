# Agent Creator Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Tech Stack

- FastAPI with async/await
- LangGraph for agent workflows
- DynamoDB for checkpoints
- S3 for checkpoint storage
- Pydantic v2 for data validation
- pytest with pytest-asyncio for testing

## Local Development Environment

**CRITICAL: Any changes that impact the local development environment MUST be reflected in `/start-dev.sh`**

This includes:
- New environment variables (add to .env generation in start-dev.sh)
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

## Python Environment

This project uses `uv` for dependency management:

```bash
# Install dependencies
uv sync

# Run tests (prefer runTests MCP tool when available)
uv run pytest tests/ -v
```

## File Writing Safety

NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits with proper file writing tools instead.

## Architecture Patterns

### Agent Creation Workflow

- Implements LangGraph workflows for creating and managing sub-agents
- Uses checkpointers for workflow persistence (DynamoDB + S3)
- Integrates with Playground Backend for creating agents
- Supports agent configuration validation and testing

### Configuration

- All configuration is loaded from environment variables via Pydantic models
- Use `SecretStr` for sensitive values
- Validate configuration at startup
- Support multiple environments (local, dev, stg, prod)

## Distributed Tracing

### Receiving Traces from Orchestrator

The agent-creator participates in distributed tracing by receiving trace context from the orchestrator.

**Implementation in `main.py`:**

```python
from langsmith.middleware import TracingMiddleware

app = server.build(lifespan=lifespan)
app.add_middleware(TracingMiddleware)  # Receives trace from orchestrator
```

**How it works:**
1. Orchestrator injects LangSmith trace headers (`langsmith-trace`, `baggage`) in HTTP requests
2. `TracingMiddleware` extracts headers and continues the trace context
3. All agent-creator operations appear as children of the orchestrator's run in LangSmith

**Requirements:**
- `LANGSMITH_API_KEY` must be configured (from AWS SSM in production)
- `LANGSMITH_TRACING=true` to enable tracing
- `LANGSMITH_ENDPOINT` and `LANGSMITH_PROJECT` must be set

**CRITICAL**: `TracingMiddleware` must be registered in the middleware stack. If missing, traces will not be connected to the orchestrator.

## File Writing Safety

NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits with proper file writing tools instead.

## Testing

**Prefer the runTests MCP tool over terminal commands when running tests.**

Fallback to direct pytest commands when needed:
- Use pytest with pytest-asyncio for async tests
- Use aiomoto for mocking AWS services
- Mock external dependencies (Playground Backend, etc.)
- Test LangGraph workflows with mock checkpointers
- Verify agent creation logic with test cases
