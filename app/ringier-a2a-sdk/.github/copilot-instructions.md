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

### Agent Base Class

- Extend `BaseAgent` for custom agent implementations
- Implement required methods: `process_request()`, `get_capabilities()`
- Use proper error handling and logging
- Support async operations

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
