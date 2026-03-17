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

### Runtime Parameters: config vs context

**CRITICAL**: LangGraph invocations use TWO distinct parameters with different purposes:

#### `config` (RunnableConfig) - Controls HOW the graph runs
Standard LangGraph/LangChain parameter containing infrastructure and observability settings:

- **Checkpoint isolation** (`configurable.thread_id`, `configurable.checkpoint_ns`)
  - Thread ID isolates conversation state: `{context_id}::agent-name`
  - Checkpoint namespace for additional isolation layer
  - `__pregel_checkpointer` for custom checkpoint backends

- **Cost tracking** (`tags: ["sub_agent:name"]`)
  - LangSmith tags for cost attribution
  - Inherited from parent and extended by sub-agents

- **Metadata** (`metadata: {user_id, assistant_id}`)
  - User/assistant attribution for LangSmith
  - Inherited from orchestrator's config

- **Callbacks** (LangChain callback handlers)
  - Logging, tracing, and monitoring hooks
  - Automatically propagated through call chain

#### `context` (GraphRuntimeContext) - Controls WHAT the graph accesses
Custom parameter enabled by `context_schema=GraphRuntimeContext` containing user-specific runtime data:

- **Tool registry** (`tool_registry: dict[str, BaseTool]`)
  - User's MCP tools discovered at request time
  - Middleware binds these to the model dynamically

- **SubAgent registry** (`subagent_registry: dict[str, CompiledSubAgent]`)
  - Available sub-agents for task delegation
  - Both local and remote A2A agents

- **User preferences** (`name`, `language`, `timezone`, `custom_prompt`)
  - Personalization data injected into system prompts
  - Message formatting preferences (markdown/slack/plain)

- **File attachments** (`pending_file_blocks: list[ContentBlock]`)
  - Ephemeral file content from current request
  - Injected into sub-agent dispatches

- **Whitelisted tools** (`whitelisted_tool_names: set[str]`)
  - Tool scope filtering (orchestrator vs GP agent)

**Why both are required:**
```python
result = await graph.ainvoke(
    {"messages": [...]},
    config=config,        # ← LangGraph infrastructure (checkpointing, tracking)
    context=context,      # ← Custom runtime data (tools, user info)
)
```

- Without `config`: No checkpoint isolation, no cost attribution, no metadata propagation
- Without `context`: No tools, no user preferences, middleware cannot function

`config` is a LangGraph standard for execution control; `context` is our custom extension for runtime data injection. They are complementary, not redundant.

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

### Checkpoint Isolation via Unique thread_id (app/agents/dynamic_agent.py)

**ALL sub-agents (dynamic AND remote) MUST use unique configurable thread_id values** to achieve complete checkpoint isolation from the orchestrator. This is the PRIMARY isolation mechanism, not `checkpoint_ns`.

Why thread_id is required:
- checkpoint_ns alone is insufficient
- Without unique thread_id, sub-agents could load orchestrator's checkpoint history, causing "missing tool_result" errors
- **CRITICAL**: All agents share the SAME DynamoDB table (`{env}-nannos-infrastructure-agents-langgraph-checkpoints`)

Thread ID patterns:
- **Dynamic sub-agents**: `{context_id}::dynamic-{agent_name}` (app/agents/dynamic_agent.py)
- **Remote A2A agents**: `{context_id}::{checkpoint_ns}` (ringier_a2a_sdk/agent/langgraph_bedrock.py)
  - agent-creator: `{context_id}::agent-creator`
  - alloy-agent: `{context_id}::alloy-agent`

Always include `__pregel_checkpointer` in config to prevent LangGraph from misinterpreting checkpoint_ns as a subgraph identifier.

### Graph Caching: One Per Model Type (app/core/graph_factory.py)

Create one LangGraph instance per model type (Claude, GPT-4), shared across all users (the model is baked into the graph). Tools (mcp tools and subagents) are injected dynamically via `DynamicToolDispatchMiddleware` which reads from `GraphRuntimeContext`, not baked into graphs. This enables memory-efficient graph reuse while maintaining user isolation via separate thread_ids.
