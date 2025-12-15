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

## Testing

- Use pytest with pytest-asyncio for async tests
- Use aiomoto for mocking AWS services
- Mock external dependencies (OpenAI, etc.)
- Test LangGraph workflows with mock checkpointers
- Verify document store operations with test database
