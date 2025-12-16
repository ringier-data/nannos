# Agent Creator Copilot Instructions

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

## Testing

- Use pytest with pytest-asyncio for async tests
- Use aiomoto for mocking AWS services
- Mock external dependencies (Playground Backend, etc.)
- Test LangGraph workflows with mock checkpointers
- Verify agent creation logic with test cases
