# Ringier A2A SDK

Current version: **v0.1.0**

A Python SDK for building authenticated agent-to-agent communication systems using the A2A (Agent-to-Agent) protocol with OIDC authentication.

## Features

- **JWT Authentication**: Validate orchestrator service tokens using JWKS
- **OAuth2 Flows**: Complete OAuth2 implementation with client credentials, token exchange (RFC 8693), and token refresh
- **User Context Propagation**: Extract user information from A2A message metadata
- **Flexible Middleware**: Support for both orchestrator JWT and user OIDC token validation
- **Agent Base Classes**: Common interfaces for building A2A agents
- **Server Components**: Context builders and executors for A2A servers
- **Common Models**: Shared data models (AgentStreamResponse, UserConfig)
- **Token Caching**: Per-audience token caching with automatic expiry handling
- **JWKS Caching**: Efficient public key caching with configurable TTL
- **OIDC Discovery**: Automatic metadata discovery from .well-known endpoints
- **Type-Safe**: Full type hints and custom exception types

## Modules

### Authentication (`ringier_a2a_sdk.auth`)

- `JWKSFetcher`: Fetch and cache JWKS keys from OIDC provider
- `JWTValidator`: Validate JWT tokens with RS256 signatures
- Custom exceptions: `InvalidIssuerError`, `ExpiredTokenError`, `InvalidAudienceError`, etc.

### OAuth2 (`ringier_a2a_sdk.oauth`)

- `OidcOAuth2Client`: OAuth2 client supporting:
  - Client credentials flow with per-audience token caching
  - RFC 8693 token exchange for service-specific tokens
  - Token refresh with rotation support
  - Automatic token expiry checking with configurable leeway
  - OIDC metadata discovery via .well-known endpoints
  - Connection pooling and proper async resource cleanup
- `BaseOAuth2Client`: Base class with OIDC metadata discovery
- Custom exceptions: `ClientCredentialsError`, `TokenExchangeError`, `TokenRefreshError`

### Middleware (`ringier_a2a_sdk.middleware`)

- `OrchestratorJWTMiddleware`: Validate orchestrator JWT tokens (fail-fast)
- `UserContextFromMetadataMiddleware`: Extract user context from A2A message metadata
- `OidcUserinfoMiddleware`: Alternative user token validation via userinfo endpoint
- `session_jwt`: Session JWT utilities

### Agent (`ringier_a2a_sdk.agent`)

- `BaseAgent`: Abstract base class for A2A agents
  - Defines `stream()` method for processing queries
  - Supports `close()` for resource cleanup

### Server (`ringier_a2a_sdk.server`)

- `BaseAgentExecutor`: Base executor for A2A agent tasks
  - Handles authentication validation
  - Manages task lifecycle
  - Streams responses from agents
- `AuthRequestContextBuilder`: Context builder with zero-trust authentication
  - Extracts user info from middleware
  - Populates call context with verified user data

### Models (`ringier_a2a_sdk.models`)

- `AgentStreamResponse`: Standard response for agent streaming operations
  - Uses A2A `TaskState` enum for status
  - Contains content and optional metadata
- `UserConfig`: User configuration for personalized agent behavior
  - User credentials (user_id, access_token)
  - User profile (name, email, language)
  - Discovered sub-agents and tools

## Installation

```bash
# Local development (from repository root)
cd app/ringier-a2a-sdk
uv pip install -e .
```

## Quick Start

### Building an A2A Agent

```python
from collections.abc import AsyncIterable
from a2a.types import Task, TaskState
from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig


class MyAgent(BaseAgent):
    async def close(self):
        """Cleanup resources."""
        pass
    
    async def stream(
        self, 
        query: str, 
        user_config: UserConfig, 
        task: Task
    ) -> AsyncIterable[AgentStreamResponse]:
        """Process query and stream responses."""
        # Emit working status
        yield AgentStreamResponse(
            state=TaskState.working,
            content="Processing your request..."
        )
        
        # Do work with user_config.access_token
        result = await self._process(query, user_config)
        
        # Emit completed status
        yield AgentStreamResponse(
            state=TaskState.completed,
            content=result
        )
```

### Setting Up A2A Server with Authentication

```python
from a2a.server.starlette import A2AServer
from a2a.types import HTTPAuthSecurityScheme
from ringier_a2a_sdk.middleware import (
    OrchestratorJWTMiddleware,
    UserContextFromMetadataMiddleware,
)
from ringier_a2a_sdk.server import AuthRequestContextBuilder, BaseAgentExecutor
import os

# Create executor with your agent
class MyAgentExecutor(BaseAgentExecutor):
    pass

agent = MyAgent()
executor = MyAgentExecutor(agent)

# Create A2A server with authentication
server = A2AServer(
    executor=executor,
    context_builder=AuthRequestContextBuilder(),
    security=[
        HTTPAuthSecurityScheme(
            type="http",
            scheme="bearer",
            bearer_format="JWT",
            description="Orchestrator JWT authentication"
        )
    ],
    scopes=[],
)

app = server.build()

# Add middleware (order matters: bottom-to-top for requests)
app.add_middleware(UserContextFromMetadataMiddleware)
app.add_middleware(
    OrchestratorJWTMiddleware,
    issuer=os.getenv("OIDC_ISSUER"),
    expected_azp=os.getenv("ORCHESTRATOR_CLIENT_ID"),
    expected_aud=os.getenv("AGENT_CLIENT_ID"),
)
```

### Agent with Orchestrator JWT Authentication (Legacy)

```python
from ringier_a2a_sdk.middleware import OrchestratorJWTMiddleware, UserContextFromMetadataMiddleware
from a2a.server.apps import A2AFastAPIApplication
import os

# Configure middleware
app = server.build()
app.add_middleware(UserContextFromMetadataMiddleware)
app.add_middleware(
    OrchestratorJWTMiddleware,
    issuer=os.getenv("OIDC_ISSUER"),
    expected_azp=os.getenv("ORCHESTRATOR_CLIENT_ID"),
    expected_aud=os.getenv("AGENT_CLIENT_ID")
)
```

### OAuth2 Client Usage

```python
from ringier_a2a_sdk.oauth import OidcOAuth2Client
import os

# Client for OAuth2 operations
oauth_client = OidcOAuth2Client(
    client_id=os.getenv("ORCHESTRATOR_CLIENT_ID"),
    client_secret=os.getenv("ORCHESTRATOR_CLIENT_SECRET"),
    issuer=os.getenv("OIDC_ISSUER"),
    token_leeway=600  # Clock skew tolerance in seconds
)

# Client credentials: Get service token for specific audience
service_token = await oauth_client.get_token(audience="agent-client-id")

# Token exchange: Exchange user token for service-specific token
exchanged_token = await oauth_client.exchange_token(
    subject_token=user_access_token,
    target_client_id="downstream-service-id",
    requested_scopes=["read", "write"]
)

# Token refresh: Refresh user's access token
refreshed = await oauth_client.refresh_token(refresh_token=user_refresh_token)
new_access_token = refreshed["access_token"]
new_refresh_token = refreshed["refresh_token"]

# Cleanup
await oauth_client.close()
```

## Environment Variables

- `OIDC_ISSUER`: OIDC issuer URL (e.g., `https://login.example.com/realms/my-realm`)
- `ORCHESTRATOR_CLIENT_ID`: Orchestrator's client ID in OIDC provider
- `ORCHESTRATOR_CLIENT_SECRET`: Orchestrator's client secret
- `AGENT_CLIENT_ID`: Agent's client ID in OIDC provider
- `JWKS_CACHE_TTL_SECONDS`: JWKS cache TTL in seconds (default: 3600)

## Documentation

- [OIDC Setup Guide](../docs/KEYCLOAK_ORCHESTRATOR_SETUP.md) (includes Keycloak example)

## Testing

```bash
cd app/ringier-a2a-sdk
pytest tests/ -v
```

## License

Copyright © 2025 Ringier AG
