# Ringier A2A SDK Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Tech Stack

- Python SDK for Agent-to-Agent (A2A) communication
- FastAPI for server implementations
- Pydantic v2 for data validation
- JWT for authentication and authorization
- pytest for testing

## Overview

This SDK provides:
- Base classes for creating A2A agents
- Server middleware for A2A protocol handling
- Authentication and authorization utilities
- OAuth integration for token management
- Request/response models for A2A communication

## Code Style

- Use async/await for all I/O operations where applicable
- Type hints are required for all function signatures
- Follow Pydantic best practices for model definitions
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

### Agent Base Class Hierarchy

The SDK provides a base agent class for building A2A agents:

**BaseAgent** (abstract)
- Root agent interface
- Implements stream() template method with cost tracking setup
- Requires `_stream_impl()` implementation
- Sets request-scoped credentials in context variables

**LangGraphBedrockAgent** (extends BaseAgent)
- Adds AWS Bedrock + LangGraph integration with MCP tools
- Implements MCP tool discovery and DynamoDB checkpointing
- Lazy loads MCP tools on first request (not during init)
- Provides default graph creation with FinalResponseSchema support
- Requires async `_get_mcp_connections()` implementation

### Server Middleware

- Use provided middleware for FastAPI applications
- Implements A2A protocol request/response handling
- Handles authentication and authorization
- Provides request validation and error handling

### Authentication

- JWT-based authentication for agent communication
- OAuth integration for token exchange
- Support for refresh tokens
- Proper token validation and expiration handling

### Credential Injection for MCP Tools

LangGraph-based agents support two credential injection strategies for MCP tool authentication:

**PassThroughCredentialInjector** (for pre-exchanged tokens):
- Used when credentials are already in the correct format for the MCP server
- Example: orchestrator pre-exchanges user token → gatana token before passing to agent
- Returns: `f"Bearer {access_token}"` directly

**TokenExchangeCredentialInjector** (for OIDC token exchange):
- Used when credentials need to be exchanged for a different audience/client
- Performs RFC 8693 OIDC token exchange at request time
- Preserves user identity through exchange
- Example: agent-creator exchanges user token for gatana token at tool-call time

Both injectors:
- Automatically handle credential injection at tool-call time via interceptor pipeline
- Extract credentials from thread-safe context variables set by `BaseAgent.stream()`
- Can be applied to initial MCP handshake by accessing credentials in `_get_mcp_connections()`

**Pattern for Initial Handshake Authentication**:
```python
async def _get_mcp_connections(self) -> dict[str, StreamableHttpConnection]:
    # get_headers() is a helper method that extracts credentials from context variables using the credential injector
    headers = await self.get_headers()
    return {
        "server": StreamableHttpConnection(
            transport="streamable_http",
            url="https://example.com/mcp",
            headers=headers,  # Credentials in initial handshake
        )
    }
````

## Continuous Interaction Turns (Steering)

The executor supports sending follow-up messages to a running agent. Understanding the underlying A2A SDK queue mechanics is critical.

### EventQueue Tap Semantics

The A2A SDK's `EventQueue.tap()` creates a child queue registered in `parent._children`. When the parent calls `enqueue_event()`, the event is placed in the parent's `asyncio.Queue` AND fan-out copied to every child:

```python
# a2a/server/events/event_queue.py
async def enqueue_event(self, event):
    await self.queue.put(event)
    for child in self._children:
        await child.enqueue_event(event)  # independent copy
```

`QueueManager.create_or_tap()` is called for **every** `send_message` request. If a queue exists for the task_id, it taps (creates a child). This is designed for `resubscribe` — reconnecting observers to an in-progress stream.

### Why This Matters for Steering

Steering messages use the same `send_message` endpoint, so the SDK automatically creates a tapped child queue. The executor emits only one ack event (`TaskStatusUpdateEvent`) into the event queue, but since the child is tapped to the parent, all ongoing parent events (artifact chunks, status updates from the primary execution) also propagate into the child.

Consumers must handle this:
- **Draining all events** (like `A2AClientRunnable.send_steering_message`) is safe when using an independent HTTP/SSE connection — the child is transport-isolated
- **Breaking after one event** (like playground-backend) is required when sharing the same SDK `Client`, to avoid leaked parent events hitting the shared `ClientTaskManager` ("Task is already set" error)

Note: In practice the child stream is very short-lived — the executor returns immediately after the ack, causing `_run_event_stream` to close the child queue. Both `pass` and `break` are functionally equivalent; the difference is defensive.

### Safety Properties

- **No cross-queue interference**: Each `EventQueue` has its own `asyncio.Queue`. Consuming from a child never removes events from the parent
- **ClientTaskManager collision**: The executor emits `TaskStatusUpdateEvent` (not a raw `Task`) for the ack to avoid "Task is already set" errors on clients that share a `Client` instance between primary and steering streams
- **Backpressure**: Undrained child queues (maxsize=1024) can block `parent.enqueue_event` if they fill up. SSE connection teardown calls `close()` on the child, which makes future `enqueue_event` calls return immediately

### Executor Implementation (`server/executor.py`)

- Active streams are tracked in `_active_streams` (module-level dict, keyed by `context_id`)
- `SteeringMiddleware` consumes pending messages from `ActiveStreamInfo.message_queue` before each LLM call
- After completion, if unconsumed messages remain, the executor re-invokes the agent once (`MAX_STEERING_REINVOCATIONS=1`)
- The `finally` block always deregisters the stream and clears the message queue

## Testing

**Prefer the runTests MCP tool over terminal commands when running tests.**

Fallback to direct pytest commands when needed:
- Use pytest for all tests
- Mock external dependencies (OAuth providers, etc.)
- Test authentication flows thoroughly
- Verify protocol compliance

## Publishing

- This is a library package, not a service
- Changes should maintain backward compatibility
- Update version following semantic versioning
- Document breaking changes clearly
