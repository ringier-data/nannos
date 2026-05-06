# Agent Creator A2A Server

An A2A (Agent-to-Agent) service that helps users design and create specialized AI agents through natural language conversation.

## Overview

The Agent Creator acts as an expert consultant that guides users through:
- Understanding agent requirements and use cases
- Choosing appropriate agent types (local, remote, foundry)
- Designing effective system prompts
- Selecting the right LLM model
- Configuring tools and capabilities
- Following naming conventions and best practices

## Features

- **Expert Guidance**: Provides best practices for agent design and configuration
- **Multi-turn Conversations**: Maintains context across interactions for iterative refinement
- **Tool Integration**: Connects to console backend for agent lifecycle management
- **Provenance Tracking**: Automatically tracks which agents create other agents
- **Naming Validation**: Enforces naming conventions (lowercase, hyphens, numbers only)

## Architecture

- **Runtime**: FastAPI with A2A protocol support
- **LLM**: Claude Sonnet 4.5 via AWS Bedrock
- **Tools**: MCP (Model Context Protocol) tools from console backend
- **Checkpointing**: DynamoDB with S3 offload for conversation persistence
- **Authentication**: JWT bearer tokens via JWTValidatorMiddleware (validates orchestrator tokens)

## Available MCP Tools

The agent has access to:
1. `console_list_sub_agents` - View existing agents
2. `console_create_sub_agent` - Create new agents
3. `console_update_sub_agent` - Modify existing agents
4. `console_grep_mcp_tools` - Discover available tools

## Running Locally

```bash
# Install dependencies
make install-dev

# Copy environment template
cp .env.template .env

# Edit .env with your configuration
# Then start the server
make app
```

The server will start on `http://localhost:8080` by default.

## Environment Variables

See `.env.template` for all configuration options. Key variables:

- `AGENT_ID`: Unique identifier for this agent (for provenance tracking)
- `CONSOLE_BACKEND_URL`: URL of the console backend MCP endpoint
- `AWS_BEDROCK_REGION`: AWS region for Bedrock access
- `CHECKPOINT_DYNAMODB_TABLE_NAME`: DynamoDB table for conversation state

## Development

```bash
# Run tests
make test

# Run linting
make lint
```

## Deployment

The agent is containerized and deployed alongside other infrastructure agents. See the main repository documentation for deployment instructions.
