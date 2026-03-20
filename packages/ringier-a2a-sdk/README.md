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

- `JWTValidatorMiddleware`: JWT token validation via local JWKS-based validation
  - Validates both user tokens and orchestrator exchanged tokens
  - Configure `expected_azp` to require tokens from specific client (e.g., orchestrator)
  - Configure `expected_aud` to require tokens targeted for specific audience
- `UserContextFromRequestStateMiddleware`: Extract user context from request.state.user (set by JWTValidatorMiddleware)

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
    
    async def _stream_impl(
        self, 
        query: str, 
        user_config: UserConfig, 
        task: Task
    ) -> AsyncIterable[AgentStreamResponse]:
        """Agent-specific streaming implementation.
        
        The base class stream() method handles:
        - Cost tracking setup
        - Sub-agent ID attribution
        - Request-scoped credentials
        
        This method focuses on agent logic only.
        """
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
    JWTValidatorMiddleware,
    UserContextFromRequestStateMiddleware,
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
            description="JWT authentication via orchestrator"
        )
    ],
    scopes=[],
)

app = server.build()

# Add middleware (order matters: bottom-to-top for requests)
app.add_middleware(UserContextFromRequestStateMiddleware)
app.add_middleware(
    JWTValidatorMiddleware,
    issuer=os.getenv("OIDC_ISSUER"),
    expected_azp=os.getenv("ORCHESTRATOR_CLIENT_ID"),  # Require tokens from orchestrator
    expected_aud=os.getenv("AGENT_CLIENT_ID"),  # Require tokens targeted for this agent
)
```

### Alternative Configuration: Sub-Agent with Required Orchestrator Authentication

```python
from ringier_a2a_sdk.middleware import JWTValidatorMiddleware, UserContextFromRequestStateMiddleware
import os

# Configure middleware to require tokens from orchestrator
app = server.build()
app.add_middleware(UserContextFromRequestStateMiddleware)
app.add_middleware(
    JWTValidatorMiddleware,
    issuer=os.getenv("OIDC_ISSUER"),
    expected_azp=os.getenv("ORCHESTRATOR_CLIENT_ID"),  # REQUIRED: Ensures tokens from orchestrator only
    expected_aud=os.getenv("AGENT_CLIENT_ID")  # Optional: Validates audience claim
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

## Cost Tracking

Track LLM usage and costs for your agent using the `CostTrackingMixin`.


### LangGraph with Checkpointing (Recommended)

For agents using LangGraph with checkpointing, use `create_runnable_config()` to automatically include cost tracking tags and callbacks:

```python
from ringier_a2a_sdk.agent import BaseAgent
from langgraph.graph.state import CompiledStateGraph
from langgraph_checkpoint_aws import DynamoDBSaver

class MyAgent(BaseAgent):
    def __init__(self):
        super().__init__()
        self.enable_cost_tracking(backend_url="https://backend.example.com")

        # Create checkpointer
        self._checkpointer = DynamoDBSaver(
            table_name="my-agent-checkpoints",
            region_name="eu-central-1"
        )

        # Build LangGraph agent
        self._graph = self._build_graph()
    
    async def _stream_impl(self, query, user_config, task):
        # Create config with cost tracking tags, callbacks, and checkpointer
        config = self.create_runnable_config(
            user_id=user_config.user_id,
            conversation_id=task.context_id,
            thread_id=task.context_id,
            checkpoint_ns="my-agent",  # Namespace isolation
            checkpointer=self._checkpointer,
        )

        # LangGraph automatically tracks costs via callbacks in config
        async for event in self._graph.astream({"messages": [query]}, config):
            # Process events...
            pass
```

**What `create_runnable_config()` does:**
- Automatically adds `user:{user_id}` and `conversation:{conversation_id}` tags
- Automatically adds `sub_agent:{sub_agent_id}` tag (from ContextVar if available)
- Includes callbacks from `get_langchain_callbacks()` for cost tracking
- Configures checkpointer parameters (`thread_id`, `checkpoint_ns`, `__pregel_checkpointer`)
- Returns `RunnableConfig` object


### Manual Instrumentation (Any Framework)

```python
class MyAgent(BaseAgent):
    def __init__(self):
        super().__init__()
        # Enable cost tracking (creates callbacks and cost logger)
        # Note: sub_agent_id will be updated from user_config via BaseAgent on first request
        self.enable_cost_tracking(backend_url="https://cost-tracking-backend.example.com")

    async def _stream_impl(self, query, user_config, task):
        # Call any LLM (OpenAI, Bedrock, etc.)
        response = await my_llm_client.invoke(query)
        
        # Manually report usage
        # Cost tracking auto-starts on first request
        await self.report_llm_usage(
            user_id=user_config.user_id,
            provider="openai",
            model_name="gpt-4o",
            billing_unit_breakdown={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens
            },
            conversation_id=task.context_id
        )
        # ... stream results
```

**Sub-Agent ID Attribution:**
- **Remote agents**: `sub_agent_id` is automatically read from `current_sub_agent_id` ContextVar (set by `SubAgentIdMiddleware`)
- **Local agents** (orchestrator dynamic agents): `sub_agent_id` is automatically extracted from LangGraph tags by `CostTrackingCallback`
- Agents don't need to pass `sub_agent_id` explicitly - the SDK handles it transparently

### Custom Billing Units (Beyond Tokens)

The cost tracking system supports **any billing unit**, not just LLM tokens. Agents can track API calls, searches, computations, or any resource usage by treating them as "billing units" in the `billing_unit_breakdown`.

#### Example: API Call-Based Billing

```python
class MyAgent(BaseAgent):
    async def _stream_impl(self, query, user_config, task):
        # Call external API
        premium_calls = await self.call_premium_api(query)
        standard_calls = await self.call_standard_api(query)
        
        # Report custom billing units
        # Cost tracking auto-starts on first request
        await self.report_llm_usage(
            user_id=user_config.user_id,
            provider="my_service",
            model_name="api-v2",
            billing_unit_breakdown={
                "premium_api_calls": premium_calls,
                "standard_api_calls": standard_calls
            },
            conversation_id=task.context_id
        )
        # ... stream results
```

#### Billing Unit Naming Guidelines

Use **snake_case** names (lowercase, underscores) that are descriptive and consistent:

- ✅ **Good**: `premium_api_calls`, `vector_searches`, `gpu_seconds`, `documents_indexed`
- ✅ **Good**: `requests_tier1`, `requests_tier2`, `storage_gb_hours`
- ❌ **Bad**: `Premium API Calls` (spaces), `apiCalls` (camelCase), `123_requests` (starts with number)
- ❌ **Reserved**: `id`, `cost`, `total`, `timestamp`, `count` (system reserved)

**Rules:**
- 3-64 characters
- Start and end with letters/numbers
- Use underscores to separate words
- Avoid names that conflict with standard token types if they mean different things

#### Pricing Configuration

Pricing for custom billing units is configured per sub-agent via the `pricing_config` field. Admins set pricing when creating or updating sub-agents:

**Cost Calculation:**
- `premium_api_calls = 1` → Cost = (1 / 1,000,000) × $50,000 = **$0.05**
- `standard_api_calls = 10` → Cost = (10 / 1,000,000) × $10,000 = **$0.10**
- **Total**: $0.15


## Documentation

- [OIDC Setup Guide](../docs/KEYCLOAK_ORCHESTRATOR_SETUP.md) (includes Keycloak example)

## Testing

```bash
cd app/ringier-a2a-sdk
pytest tests/ -v
```

## License

Copyright © 2025 Ringier AG
