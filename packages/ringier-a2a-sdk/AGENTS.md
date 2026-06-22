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

### Layering: this SDK is the lowest shared layer

`agent-common` and all the apps (orchestrator, agent-runner, console-backend) depend on
this SDK — **never the reverse**. The SDK must not import `agent_common.*` (or any app
package). A violation isn't just stylistic: console-backend uses the SDK without installing
agent-common, so an upward import crashes it at runtime (e.g. a `ModuleNotFoundError` in the
catalog re-index embedding path). When the SDK needs something currently sitting in
agent-common, move the canonical copy down here and let agent-common re-export or import it.

### Model Gateway / cost cluster lives here by design

`cost_tracking/` (logger, callback), `embeddings.py` (`GatewayEmbeddings`), and
`cost_tracking/attribution.py` (spend-logs ContextVars + `attribution_header` /
`build_attribution_http_client`) are part of the SDK on purpose, not internal code that
leaked in: remote/external agents can opt into routing LLM + embedding traffic through the
Model Gateway to get spend-logs attribution (and, later, extra-cost reporting). This
is why they sit behind the `google-embeddings` / `langgraph` extras rather than the core
deps. Don't propose extracting this cluster out of the SDK. Attribution's single canonical
import path is `ringier_a2a_sdk.cost_tracking.attribution`.

### Agent Base Class Hierarchy

The SDK provides a base agent class for building A2A agents:

**BaseAgent** (abstract)
- Root agent interface
- Implements stream() template method with cost tracking setup
- Requires `_stream_impl()` implementation
- Sets request-scoped credentials in context variables

**LangGraphBedrockAgent** (extends BaseAgent)
- Adds AWS Bedrock + LangGraph integration with MCP tools
- Implements MCP tool discovery and PostgreSQL checkpointing (see Checkpointing below)
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
- Example: an agent exchanges the user token for a gatana token at tool-call time

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

### Checkpointing (PostgreSQL + optional S3 offload)

LangGraph state is persisted by `PostgreSQLCheckpointerMixin`
([`agent/postgres_checkpointer_mixin.py`](ringier_a2a_sdk/agent/postgres_checkpointer_mixin.py)).

- **Storage.** Reuses the service's `POSTGRES_*` connection (the docstore DB; tables land in
  the schema on the connection's `search_path`, `public` locally). Standard LangGraph tables:
  `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`.
- **Thread IDs.** A top-level agent checkpoints under the bare `context_id` (empty
  `checkpoint_ns`); a sub-agent checkpoints under `{context_id}::{agent-name}` (e.g.
  `…::voice-agent`). Each hop resumes its own checkpoint in its own namespace.
- **MemorySaver fallback.** When `POSTGRES_HOST` is unset the mixin falls back to an in-memory
  saver — **local dev only**. A fallback in a real environment silently drops persisted state
  (and any pending HITL decision), so it is gated by `CHECKPOINT_ALLOW_MEMORY` outside local.
- **S3 offload.** `S3OffloadingSerde` wraps `JsonPlusSerializer`: when a serialized blob exceeds
  `CHECKPOINT_S3_THRESHOLD_MB` (**default 1 MB**) it is uploaded to `CHECKPOINT_S3_BUCKET_NAME`
  and the DB row stores a compact `s3ref` reference `{"s3_key", "original_type"}` instead; the
  read path fetches it back on demand. Offloading is disabled when the bucket env is unset.
  Caveat: the serde uses a bare `boto3.client("s3")` with no endpoint override, so it always
  hits real AWS — there is **no** Localstack/`AWS_S3_ENDPOINT_URL` redirect. Offloaded objects
  are **not** deleted automatically (lifecycle is the bucket's responsibility).
- **Tests.** `tests/test_postgres_checkpointer_s3_offload.py` (serde offload/round-trip/error
  paths, mocked S3 + an opt-in `WITH_REAL_S3` suite) and `tests/test_postgres_checkpointer_mixin.py`
  (`_build_serde`, pool construction, MemorySaver fallback, version check, lifecycle).

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
