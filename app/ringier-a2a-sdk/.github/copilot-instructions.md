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
