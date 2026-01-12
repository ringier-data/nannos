# Orchestrator Agent Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

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

## Python Environment

This project uses `uv` for dependency management:

```bash
# Install dependencies
uv sync

# Run Python commands
uv run python script.py

# Run tests (prefer runTests MCP tool when available)
uv run pytest tests/ -v

# Run with coverage
uv run pytest tests/ --cov=app --cov-report=html
```

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

## File Writing Safety

NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits with proper file writing tools instead.

## Testing

**Prefer the runTests MCP tool over terminal commands when running tests.**

Fallback to direct pytest commands when needed:
- Use pytest with pytest-asyncio for async tests
- Use aiomoto for mocking AWS services
- Mock external dependencies (OpenAI, etc.)
- Test LangGraph workflows with mock checkpointers

## Critical Design Decisions

### Dynamic Trace Header Injection (app/subagents/runnable.py)

LangSmith trace headers MUST be injected dynamically per-request using httpx event hooks, not at client initialization. Use `event_hooks={"request": [self._inject_trace_headers]}` where the hook calls `get_current_run_tree().to_headers()` to get current trace context. Static injection would use stale context.

### Checkpoint Namespace Isolation (app/backends/dynamodb_checkpointer.py)

Orchestrator and sub-agents share the same DynamoDB checkpoint table and the same context_id (conversation_id / thread_id) but use `checkpoint_ns` to isolate conversation histories. Without this, MCP tool calls from sub-agents would appear in orchestrator conversations. Always include `__pregel_checkpointer` in config to prevent LangGraph from misinterpreting the namespace.

### Graph Caching: One Per Model Type (app/core/graph_factory.py)

Create one LangGraph instance per model type (Claude, GPT-4), shared across all users (the model is baked into the graph). Tools (mcp tools and subagents) are injected dynamically via `DynamicToolDispatchMiddleware` which reads from `GraphRuntimeContext`, not baked into graphs. This enables memory-efficient graph reuse while maintaining user isolation via separate thread_ids.
