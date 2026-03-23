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

The orchestrator propagates LangSmith trace context to all A2A sub-agents (agent-creator, alloy-agent) for unified distributed tracing. This works through two mechanisms: **in-process** `contextvars` propagation and **cross-process** HTTP header injection.

**Trace context flow:**

1. **`@traceable` creates a run span** (`app/middleware/dynamic_tool_dispatch.py`): The `@traceable(name=f"task:{subagent_type}")` decorator on `astream_a2a_agent()` creates a LangSmith `RunTree` and stores it in a Python `contextvars.ContextVar`. This context automatically propagates through all `await` chains.

2. **`astream()` is called inside the traced context**: `runnable.astream(agent_state, agent_config)` enters `A2AClientRunnable.astream()`. Because this is a normal `await`/`async for`, the `contextvars` context — including the `RunTree` — is inherited.

3. **httpx event hook injects headers** (`agent_common/a2a/client_runnable.py`): Before each HTTP request, the `_inject_trace_headers()` hook calls `get_current_run_tree()` — reading from the **same `contextvars.ContextVar`** — and injects `langsmith-trace` and `baggage` headers.

4. **Sub-agent receives headers**: `TracingMiddleware` on the remote FastAPI app extracts headers and continues the trace, making all sub-agent operations appear as child spans.

**Implementation details:**

- `A2AClientRunnable._inject_trace_headers()`: Async httpx event hook registered via `event_hooks={"request": [self._inject_trace_headers]}`
- Uses `get_current_run_tree().to_headers()` to get trace context at request time from `contextvars`
- `astream()` and `ainvoke()` accept an optional `config` parameter for interface consistency with LangChain's `Runnable`, but the remote client's tracing relies on `contextvars` + httpx hooks, not the config's callback manager

**CRITICAL**: Do NOT inject trace headers at client initialization time. Headers must be injected dynamically for each request using event hooks, otherwise trace context will be stale or missing.

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

## A2A Extensions Protocol

The orchestrator implements three custom A2A extensions for structured streaming, declared in the Agent Card capabilities and emitted as typed events on status updates and artifacts. Clients opt in by sending an `X-A2A-Extensions` header listing desired extension URIs.

### Extension URIs (app/core/a2a_extensions.py)

| URI | Transport | Purpose |
|-----|-----------|----------|
| `urn:nannos:a2a:activity-log:1.0` | `status-update` with `Message.extensions` | Tool-usage and delegation events for a timeline |
| `urn:nannos:a2a:work-plan:1.0` | `status-update` with `Message.extensions` + `DataPart` | Structured todo checklist progress |
| `urn:nannos:a2a:intermediate-output:1.0` | `artifact-update` with `Artifact.extensions` | Streaming draft content from sub-agents |

### Extension Activation (opt-in)

Clients send `X-A2A-Extensions: urn:nannos:a2a:activity-log:1.0, urn:nannos:a2a:work-plan:1.0` to receive only those event types. If the header is absent, **all extensions are disabled** (per A2A spec — extensions are opt-in). The executor checks activation via a helper:

```python
def _ext_active(uri: str) -> bool:
    return active_extensions is not None and uri in active_extensions
```

### How Events Are Emitted (app/core/executor.py)

The executor's `_handle_stream_item()` reads metadata flags from the LangGraph custom stream and maps them to A2A events:

- **`metadata["activity_log"]`** → `update_status(working, new_activity_log_message(text, source=...))` — skipped if extension not active
- **`metadata["work_plan"]`** → `update_status(working, new_work_plan_message(todos))` — skipped if extension not active
- **`metadata["intermediate_output"]`** → `add_artifact(...)` with `extensions=[INTERMEDIATE_OUTPUT_EXTENSION]` on a separate artifact ID (`{id}-thought`) — extension tag stripped if not active, but content still delivered
- **`metadata["streaming_chunk"]`** → main artifact `append=True` (no extension, always sent)

### Message Builders (app/core/a2a_extensions.py)

- `new_activity_log_message(text, source?)` → `Message(extensions=[ACTIVITY_LOG_EXTENSION], parts=[TextPart])` with optional `metadata.source` for sub-agent attribution
- `new_work_plan_message(todos)` → `Message(extensions=[WORK_PLAN_EXTENSION], parts=[DataPart({"todos": [...]})])` using `TodoItem.model_dump()`

### Agent Card Declaration (main.py)

Extensions are declared in `AgentCapabilities.extensions` so clients can discover them via `GET /.well-known/agent.json`. Each `AgentExtension` has a `uri` and `description`.

### When Adding New Extensions

1. Define the URI constant in `app/core/a2a_extensions.py`
2. Add to `ALL_EXTENSIONS` list
3. Create a message builder function if needed
4. Add metadata detection + emission logic in `executor.py` `_handle_stream_item()`
5. Declare in Agent Card capabilities in `main.py`
6. Update playground-backend event filtering in `app.py`
7. Update playground-frontend event handling in `ChatContext.tsx`

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

### Middleware Stack Execution Order (app/core/graph_factory.py)

The orchestrator uses a middleware stack assembled in `_create_middleware_stack()`. LangChain middleware hooks follow these execution rules:

- **`before_*` hooks**: First to last (list order)
- **`after_*` hooks**: Last to first (reverse order)
- **`wrap_*` hooks**: Nested — first middleware in the list wraps all others (outermost)

The current stack (outermost → innermost for `wrap_*` hooks):

| Index | Middleware | Purpose |
|-------|-----------|---------|
| `[0]` | `DynamicToolDispatchMiddleware` | Tool routing, A2A sub-agent dispatch |
| `[1]` | `StoragePathsInstructionMiddleware` | Filesystem paths in system prompt |
| `[2]` | `BedrockPromptCachingMiddleware` | Prompt caching breakpoints |
| `[3]` | `UserPreferencesMiddleware` | Language, custom prompt injection |
| `[4]` | `LoopDetectionMiddleware` | Detects infinite tool-call loops |
| `[5]` | `AuthErrorDetectionMiddleware` | Detects 401/auth errors, calls `interrupt()` |
| `[6]` | `ToolRetryMiddleware` | Retries transient tool failures |
| `[7]` | `A2ATrackingMiddleware` | Tracks sub-agent status/artifacts |
| `[8]` | `TodoStatusMiddleware` | Intercepts `write_todos` for work-plan events |

**Critical consequence**: `DynamicToolDispatchMiddleware` ([0], outermost) short-circuits the `task` tool for A2A sub-agent dispatch. Inner middlewares (Auth, Retry, etc.) never see the `task` call — they only see the returned ToolMessage. This means `AuthErrorDetectionMiddleware` cannot intercept A2A 401 errors via `interrupt()`. Those errors propagate as normal ToolMessages and the LLM handles them.

**`interrupt()` caveat**: When `interrupt()` raises `GraphBubbleUp` from inside a middleware `awrap_tool_call`, ToolNode's `_arun_one` catches it via `except Exception` and converts it to an error ToolMessage. The interrupt only propagates correctly from inside `_execute_tool_async` where `GraphBubbleUp` is explicitly re-raised. For parallel tool calls (`asyncio.gather`), all tool calls run through independent middleware chains concurrently.

**Response tool exclusion**: `AuthErrorDetectionMiddleware` skips `FinalResponseSchema` and `SubAgentResponseSchema` to prevent false-positive interrupts when the LLM merely *reports* an upstream 401 error.

### Streaming Token Leak Prevention for Utility Models

**Any LLM model created for internal/utility purposes (not the main orchestrator model) MUST use `streaming=False`.**

When the orchestrator streams with `stream_mode=["custom", "messages"]` and `version="v2"`, LangGraph captures ALL LLM token events from the entire call stack via callback propagation. Models created with `streaming=True` (the default) emit per-token callback events even during `model.ainvoke()`. These events bubble up through the orchestrator's stream and appear as raw tokens in the UI. Additionally, the `langgraph_node` metadata filter in `agent.py` blocks tokens from nodes other than `"model"`, but utility models inherit the parent node's metadata making them indistinguishable.

Affected utility models:
- **File-analyzer model** (`app/agents/file_analyzer.py`): Analysis LLM call
- **File-filtering model** (`app/middleware/dynamic_tool_dispatch.py`): LLM-based file relevance filtering
- Any future internal LLM calls made from middleware or local sub-agents

Why metadata filtering doesn't work as an alternative:
- Utility models are standalone `model.ainvoke()` calls, NOT LangGraph nodes
- They inherit the parent node's `langgraph_node` metadata (e.g., `"tools"`), making them indistinguishable from orchestrator tokens
- Filtering by `ls_model_name` would be fragile (user might choose the same model)

**Rule**: Always pass `streaming=False` to `create_model()` for any model that is not the orchestrator's or a sub-graph's primary LLM.

### LocalA2ARunnable._astream_impl Must Be an Async Generator (agent_common/a2a/base.py)

The base `_astream_impl` method MUST contain a `yield` statement to make it an async generator function, even though it only raises `NotImplementedError`. Without `yield`, Python treats it as a plain coroutine — calling it returns a coroutine object (no `__aiter__`), and `async for` in `astream()` raises `TypeError` *before* the `NotImplementedError` body executes, bypassing the fallback-to-`ainvoke` logic.

Pattern:
```python
async def _astream_impl(self, ...) -> AsyncIterable[Dict]:
    raise NotImplementedError(...)
    yield  # Makes this an async generator so NotImplementedError is raised during iteration
```

This ensures non-streaming sub-agents (like `FileAnalyzerRunnable`) gracefully fall back to `ainvoke` via the `except NotImplementedError` handler in `astream()`.

### LangGraph v2 Streaming Format (all agents)

**ALL LangGraph `astream()` calls MUST use `version="v2"`.**

v2 produces unified `StreamPart` dicts `{"type": "messages"|"custom"|"values"|"updates", "ns": tuple, "data": ...}` instead of raw `(mode, event)` tuples. This applies to:
- Orchestrator `stream()` in `app/core/agent.py`: `stream_mode=["custom", "messages"], version="v2"`
- GP agent and dynamic agent `_astream_impl()`: `stream_mode="updates", version="v2"`
- SDK `LangGraphBedrockAgent._stream_impl()`: `stream_mode="updates", version="v2"`
- Agent-runner `core.py`: `stream_mode="values", version="v2"`

### Custom Stream Writer via `get_stream_writer()` (app/middleware/)

**Use `get_stream_writer()` from `langgraph.config` to emit custom stream events from middleware and tool dispatch.** Do NOT use the undocumented `request.runtime.stream_writer` internal attribute. `get_stream_writer()` is contextvars-based and automatically available inside LangGraph node execution.
