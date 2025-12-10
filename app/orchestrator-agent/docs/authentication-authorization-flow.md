# Orchestrator Agent: Authentication & Authorization Flow

This document describes the complete authentication and authorization architecture of the Orchestrator Agent, including how credentials flow through the system, caching strategies, and limitations for multi-human scenarios.

## Table of Contents

1. [Overview](#overview)
2. [Inbound Authentication (User → Orchestrator)](#inbound-authentication-user--orchestrator)
   - [OidcUserinfoMiddleware](#oidcuserinfomiddleware)
   - [UserContextFromRequestStateMiddleware](#usercontextfromrequeststatemiddleware)
   - [AuthRequestContextBuilder](#authrequestcontextbuilder)
3. [Internal Flow (RequestContext → Agent Execution)](#internal-flow-requestcontext--agent-execution)
4. [Outbound Authentication (Orchestrator → Sub-agents)](#outbound-authentication-orchestrator--sub-agents)
   - [SmartTokenInterceptor](#smarttokeninterceptor)
   - [Token Exchange (RFC 8693)](#token-exchange-rfc-8693)
   - [Client Credentials Flow (JWT)](#client-credentials-flow-jwt)
5. [MCP Tools Authentication](#mcp-tools-authentication)
6. [Graph Construction, Checkpointing & Caching](#graph-construction-checkpointing--caching)
   - [Graph Caching Strategy](#graph-caching-strategy)
   - [Checkpointing Architecture](#checkpointing-architecture)
   - [Credentials Handling](#credentials-handling)
7. [Complete Authentication Flow Diagram](#complete-authentication-flow-diagram)
8. [Limitations for Multi-Human Scenarios](#limitations-for-multi-human-scenarios)

---

## Overview

The Orchestrator Agent implements a **Zero-Trust Authentication Architecture** where:

- User identity is validated at the edge via OIDC
- Verified credentials are propagated through context variables
- Each downstream call (sub-agents, MCP tools) uses service-specific tokens via token exchange
- Credentials are **never** persisted in checkpoints
- Graphs are cached by model type, tools are injected at runtime via `GraphRuntimeContext`

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR AGENT                                   │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  [User Request + Bearer Token]                                               │
│           │                                                                  │
│           ▼                                                                  │
│  ┌─────────────────────────────┐                                             │
│  │  OidcUserinfoMiddleware     │  ← Validates JWT via OIDC userinfo endpoint │
│  │  (Session JWT caching)      │  ← Issues session JWT cookie for caching    │
│  └─────────────────────────────┘                                             │
│           │                                                                  │
│           ▼                                                                  │
│  ┌─────────────────────────────────────┐                                     │
│  │ UserContextFromRequestStateMiddleware│ ← Transfers user to ContextVar     │
│  └─────────────────────────────────────┘                                     │
│           │                                                                  │
│           ▼                                                                  │
│  ┌─────────────────────────────┐                                             │
│  │  AuthRequestContextBuilder  │  ← Builds RequestContext with user info     │
│  └─────────────────────────────┘                                             │
│           │                                                                  │
│           ▼                                                                  │
│  ┌─────────────────────────────┐                                             │
│  │  OrchestratorDeepAgentExecutor │  ← Extracts user_id, token from context  │
│  │  + GraphFactory             │  ← Gets graph by model type (cached)        │
│  │  + GraphRuntimeContext      │  ← Injects tools/subagents at runtime       │
│  └─────────────────────────────┘                                             │
│           │                                                                  │
│           ├────────────────────┬───────────────────────┐                     │
│           ▼                    ▼                       ▼                     │
│  ┌────────────────┐   ┌────────────────┐   ┌─────────────────────┐           │
│  │  Sub-Agent A   │   │  Sub-Agent B   │   │  MCP Tools (Gatana) │           │
│  │(Token Exchange)│   │(Client Creds)  │   │  (Token Exchange)   │           │
│  └────────────────┘   └────────────────┘   └─────────────────────┘           │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Inbound Authentication (User → Orchestrator)

### OidcUserinfoMiddleware

**Location:** `ringier_a2a_sdk/middleware/oidc_userinfo_middleware.py`

**Purpose:** Validates incoming bearer tokens and caches user information in session JWTs.

**Flow:**

```
1. Request arrives with Authorization: Bearer <user_token>
2. Check for valid session JWT cookie (fast path - no network call)
3. If no valid session JWT:
   a. Call OIDC userinfo endpoint with bearer token
   b. Extract user info (sub, email, name)
   c. Create session JWT and set as HttpOnly cookie
4. Store user info in request.state.user
```

**Session JWT Caching:**

```python
# Session JWT contains cached user info from OIDC validation
payload = {
    "iss": "agent-session",
    "sub": userinfo.get("sub"),        # User ID
    "iat": now,                         # Issued at
    "exp": expiry,                      # Expires in 15 minutes (configurable)
    "email": userinfo.get("email"),
    "name": userinfo.get("name"),
    "session_type": "oidc_cached",
}
```

**Benefits:**
- Eliminates repeated OIDC userinfo calls during session
- Session JWT expires in 15 minutes (configurable via `JWT_SESSION_EXPIRY_MINUTES`)
- HttpOnly + Secure + SameSite cookies for XSS/CSRF protection

**Public Endpoints (no authentication required):**
- `/.well-known/agent-card.json`
- `/health`
- `/docs`
- `/openapi.json`

### UserContextFromRequestStateMiddleware

**Location:** `ringier_a2a_sdk/middleware/user_context_middleware.py`

**Purpose:** Transfers authenticated user info from `request.state.user` to an async-safe `ContextVar`.

**Flow:**

```python
# Extract verified user info from upstream OIDC middleware
user_data = request.state.user
user_context = {
    "user_id": user_data.get("sub"),      # Primary identifier
    "email": user_data.get("email"),
    "name": user_data.get("name"),
    "token": user_data.get("token"),      # Original bearer token
    "scopes": user_data.get("scopes", []),
}
current_user_context.set(user_context)
```

**Why ContextVar?**
- Thread-safe and async-safe
- Each request gets isolated copy
- Accessible by downstream components without passing through function parameters

### AuthRequestContextBuilder

**Location:** `ringier_a2a_sdk/server/context_builder.py`

**Purpose:** Builds the A2A `RequestContext` with verified user information from the ContextVar.

**Zero-Trust Pattern:**
```python
# Extract verified user from context variable (set by middleware after JWT validation)
user_context = current_user_context.get()

# Store in call_context.state for use by AgentExecutor
call_context.state["user_id"] = user_context["user_id"]
call_context.state["user_email"] = user_context.get("email")
call_context.state["user_name"] = user_context.get("name")
call_context.state["user_token"] = user_context.get("token")  # Original token
call_context.state["user_scopes"] = user_context.get("scopes", [])
```

---

## Internal Flow (RequestContext → Agent Execution)

**Location:** `app/core/executor.py`

The `OrchestratorDeepAgentExecutor` extracts verified user information from the `RequestContext`:

```python
# ZERO-TRUST: Extract from call_context (set by RequestContextBuilder)
if context.call_context and hasattr(context.call_context, "state"):
    try:
        user_id = context.call_context.state["user_id"]
        user_token = context.call_context.state["user_token"]
        user_name = context.call_context.state["user_name"]
        user_email = context.call_context.state["user_email"]
    except KeyError as e:
        logger.error(f"[ZERO-TRUST] Missing expected user context key: {e}")
        raise ServerError(error=InvalidParamsError()) from e

# Create UserConfig with verified credentials
user_config = UserConfig(
    user_id=user_id,
    access_token=user_token,  # SecretStr - never logged
    name=user_name,
    email=user_email,
    model=model_choice,
    message_formatting=context.message.metadata.get("message_formatting", "markdown"),
    slack_user_handle=context.message.metadata.get("slack_user_handle"),
)

# Get graph for model type (shared across users)
graph = await agent.get_or_create_graph(user_config.model)

# Convert UserConfig to GraphRuntimeContext for runtime injection
runtime_context = user_config.to_runtime_context()

# Execute with runtime context (tools/subagents injected dynamically)
async for event in agent.stream(query, user_config, context_id):
    ...
```

---

## Outbound Authentication (Orchestrator → Sub-agents)

### SmartTokenInterceptor

**Location:** `app/authentication/interceptor.py`

**Purpose:** Auto-detects authentication requirements from target `AgentCard` and applies the appropriate authentication strategy.

**Detection Logic:**

```python
def _detect_auth_scheme(self, agent_card: AgentCard) -> tuple[str, str, Any]:
    for scheme_name, scheme in (agent_card.security_schemes or {}).items():
        # Check for JWT bearer authentication
        if scheme.root.type == "http":
            if scheme.scheme == "bearer" and scheme.bearer_format == "JWT":
                return ("jwt", scheme_name, scheme)
        
        # Check for OpenID Connect (requires token exchange)
        if scheme.root.type == "openIdConnect":
            return ("oidc", scheme_name, scheme.root)
    
    raise ValueError("No supported security scheme found")
```

### Token Exchange (RFC 8693)

**Used when:** Target agent's `AgentCard` specifies `openIdConnect` security scheme.

```python
async def _handle_oidc_auth(self, agent_card, scheme_name, ...):
    # Extract required scopes from agent card
    required_scopes = []
    for security in agent_card.security or []:
        if scheme_name in security:
            required_scopes.extend(security[scheme_name])
    
    # Perform RFC 8693 token exchange
    exchanged_token = await self.oauth2_client.exchange_token(
        subject_token=self.user_token,           # User's original token
        target_client_id=scheme_name,            # Target agent's client ID
        requested_scopes=required_scopes,
    )
    
    http_kwargs["headers"]["Authorization"] = f"Bearer {exchanged_token}"
```

**Token Exchange Parameters (RFC 8693):**
```python
params = {
    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
    "subject_token": user_token,
    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
    "audience": target_client_id,  # Target service's OAuth2 client ID
    "scope": "openid profile email",  # Requested scopes
}
```

### Client Credentials Flow (JWT)

**Used when:** Target agent's `AgentCard` specifies HTTP bearer with JWT format.

```python
async def _handle_jwt_auth(self, agent_card, scheme_name, ...):
    # Get token using orchestrator's client credentials
    token = await self.oauth2_client.get_token(audience=target_client_id)
    
    http_kwargs["headers"]["Authorization"] = f"Bearer {token}"
    
    # Inject user context into message metadata (for attribution)
    self._inject_user_context(request_payload)
```

**User Context Injection:**
```python
request_payload["params"]["metadata"]["user_context"] = {
    "user_id": self.user_context.get("user_id"),
    "email": self.user_context.get("email"),
    "name": self.user_context.get("name"),
}
```

---

## MCP Tools Authentication

**Location:** `app/core/discovery.py`

MCP gateway (Gatana) require token exchange to the `mcp-gateway` service:

```python
async def discover_tools(self, token: str, white_list: List[str] = None):
    # Exchange user token for mcp-gateway-specific token
    mcp_gateway_token = await self.oauth2_client.exchange_token(
        subject_token=token,
        target_client_id="mcp-gateway",
        requested_scopes=["openid", "profile", "offline_access"],
    )
    
    # Use exchanged token for MCP connection
    client = MultiServerMCPClient(
        connections={
            "gatana": StreamableHttpConnection(
                transport="streamable_http",
                url="https://alloych.gatana.ai/mcp",
                headers={"Authorization": f"Bearer {mcp_gateway_token}"},
            )
        }
    )
    
    return await client.get_tools()
```

---

## Graph Construction, Checkpointing & Caching

### Graph Caching Strategy

**Location:** `app/core/graph_factory.py`, `app/core/agent.py`

Graphs are cached by **model type only** (not by user or capability set):

```python
class GraphFactory:
    """Factory for creating and managing LangGraph instances.
    
    Architecture:
    - ONE graph per model type, shared across all users
    - Single checkpointer for conversation continuity across model switches
    - Tools injected at runtime via GraphRuntimeContext
    - DynamicToolDispatchMiddleware handles tool binding and dispatch
    """
    
    def __init__(self, config, thinking=False, a2a_middleware=None):
        # Model and graph caches
        self._models: dict[str, BaseChatModel] = {}
        self._graphs: dict[str, CompiledStateGraph] = {}
        
        # Shared checkpointer for all graphs
        self._checkpointer = DynamoDBSaver(...)
    
    def get_graph(self, model_type: ModelType) -> CompiledStateGraph:
        """Get or create a graph for the given model type."""
        if model_type not in self._graphs:
            self._graphs[model_type] = self._create_graph(model_type)
        return self._graphs[model_type]
```

**Key Architecture Decisions:**
- **ONE universal graph per model type** (`gpt4o` or `claude-sonnet-4.5`)
- Tools are NOT "baked" into graphs - they come from `GraphRuntimeContext` at runtime
- `DynamicToolDispatchMiddleware` intercepts model calls and binds user-specific tools
- User isolation is achieved via `thread_id` in the checkpointer
- Multiple users share the same graph instance, customized via runtime context

### Checkpointing Architecture

**Checkpointer:** `DynamoDBSaver` (langgraph_checkpoint_dynamodb)

```python
self._checkpointer = DynamoDBSaver(
    DynamoDBConfig(
        table_config=DynamoDBTableConfig(
            table_name=config.CHECKPOINT_DYNAMODB_TABLE_NAME,
            ttl_days=config.CHECKPOINT_TTL_DAYS,  # Configurable via AgentSettings
        ),
        region_name=config.CHECKPOINT_AWS_REGION,
        max_retries=config.CHECKPOINT_MAX_RETRIES,
    ),
    deploy=False,
)
```

**What's Checkpointed:**
- Conversation messages (HumanMessage, AIMessage)
- Task state (working, completed, input_required, etc.)
- Todo list state
- Interrupt state (for resumption)

**What's NOT Checkpointed:**
- User credentials (tokens)
- Tools and subagent definitions (injected at runtime via `GraphRuntimeContext`)
- OAuth tokens
- Graph structure

### Middleware Stack Architecture

**Location:** `app/core/graph_factory.py`

The middleware stack is applied to all graphs:

```python
def _create_middleware_stack(self, is_bedrock: bool) -> list:
    """Create the complete middleware stack for a graph."""
    
    # Static tools for Bedrock (FinalResponseSchema)
    static_tools = [_create_final_response_tool()] if is_bedrock else []
    
    # Order: DynamicToolDispatch → UserPreferences → Auth → Retry → A2A → Todo
    return [
        DynamicToolDispatchMiddleware(static_tools=static_tools),  # 1. Binds tools at runtime
        UserPreferencesMiddleware(),        # 2. Injects user preferences into system prompt
        self._auth_middleware,              # 3. AuthErrorDetection
        self._retry_middleware,             # 4. ToolRetry with backoff
        self._a2a_middleware,               # 5. A2ATaskTracking
        self._todo_middleware,              # 6. TodoStatus
    ]
```

**Key Middleware:**
- `DynamicToolDispatchMiddleware`: Intercepts model calls, binds tools from `GraphRuntimeContext.tool_registry`
- `UserPreferencesMiddleware`: Injects language and formatting preferences into system prompt
- Middleware instances are shared across all graphs (stateless design)

### Credentials Handling

**Critical Design Principle:** Credentials are **injected at runtime**, not stored in checkpoints.

```python
# GraphRuntimeContext carries user-specific data at runtime
runtime_context = user_config.to_runtime_context()

# Graph execution with runtime context
config = {
    "configurable": {
        "thread_id": context_id,  # Only thread_id is checkpointed
    }
}

# Credentials are in runtime_context, NOT in the checkpoint
async for event in graph.astream(input_data, config, stream_mode="custom", context=runtime_context):
    ...
```

**GraphRuntimeContext Structure:**

```python
class GraphRuntimeContext(BaseModel):
    """Runtime context injected at execution time, NOT stored in checkpoints."""
    
    user_id: str
    name: str
    email: str
    language: str = "en"
    message_formatting: Literal["markdown", "slack", "plain"] = "markdown"
    slack_user_handle: Optional[str] = None
    tool_registry: dict[str, BaseTool] = {}      # User's available tools
    subagent_registry: dict[str, Any] = {}       # User's available sub-agents
```

**Token Caching in OidcOAuth2Client:**

```python
class OidcOAuth2Client:
    def __init__(self, ...):
        self.token_leeway = 600  # 10 minutes before expiry
        self._token_cache: Dict[str, OAuth2Token] = {}
    
    async def get_token(self, audience: str) -> str:
        # Check cache with expiry
        if audience in self._token_cache:
            cached = self._token_cache[audience]
            if not cached.is_expired(leeway=self.token_leeway):
                return cached["access_token"]
        
        # Fetch new token
        token = await client.fetch_token(audience=audience)
        self._token_cache[audience] = token
        return token["access_token"]
```

---

## Complete Authentication Flow Diagram

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                              COMPLETE AUTH FLOW                                   │
└──────────────────────────────────────────────────────────────────────────────────┘

[User Browser/Client]
        │
        │ POST /tasks with Authorization: Bearer <OIDC_TOKEN>
        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ OidcUserinfoMiddleware                                                           │
│ ┌─────────────────────────────────────────────────────────────────────────────┐ │
│ │ 1. Check for session JWT cookie (a2a_session)                               │ │
│ │    ├─ Valid → Use cached user info (no network call)                        │ │
│ │    └─ Invalid/Missing → Call OIDC userinfo endpoint                         │ │
│ │ 2. Store in request.state.user: {sub, email, name, token}                   │ │
│ │ 3. Create/refresh session JWT cookie (15 min TTL)                           │ │
│ └─────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ UserContextFromRequestStateMiddleware                                            │
│ ┌─────────────────────────────────────────────────────────────────────────────┐ │
│ │ 1. Read request.state.user                                                  │ │
│ │ 2. Set current_user_context ContextVar                                       │ │
│ │    {user_id, email, name, token, scopes}                                    │ │
│ └─────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ A2A DefaultRequestHandler                                                        │
│ ┌─────────────────────────────────────────────────────────────────────────────┐ │
│ │ AuthRequestContextBuilder.build()                                           │ │
│ │ 1. Read current_user_context ContextVar                                      │ │
│ │ 2. Store in call_context.state:                                             │ │
│ │    - user_id (from 'sub')                                                   │ │
│ │    - user_token (original OIDC token)                                       │ │
│ │    - user_email, user_name, user_scopes                                     │ │
│ └─────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ OrchestratorDeepAgentExecutor                                                    │
│ ┌─────────────────────────────────────────────────────────────────────────────┐ │
│ │ 1. Extract from context.call_context.state:                                 │ │
│ │    user_id, user_token, user_name, user_email                               │ │
│ │ 2. Create UserConfig(access_token=SecretStr(user_token), ...)               │ │
│ │ 3. Get graph by model type: await agent.get_or_create_graph(model_type)     │ │
│ │ 4. Convert to runtime context: user_config.to_runtime_context()             │ │
│ │ 5. Execute: agent.stream(query, user_config, context_id)                    │ │
│ └─────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────┘
        │
        ├─────────────────────────────────┬─────────────────────────────────┐
        ▼                                 ▼                                 ▼
┌───────────────────────┐  ┌───────────────────────┐  ┌───────────────────────┐
│ AgentDiscoveryService │  │ ToolDiscoveryService  │  │ GraphFactory          │
│ register_agents()     │  │ discover_tools()      │  │ get_graph(model_type) │
│                       │  │                       │  │                       │
│ For each sub-agent:   │  │ Token Exchange to     │  │ ONE graph per model:  │
│ ┌───────────────────┐ │  │ mcp-gateway:          │  │ ┌───────────────────┐ │
│ │SmartTokenInterceptor│ │  │                       │  │ │ gpt4o             │ │
│ │                   │ │  │ oauth2_client         │  │ │ claude-sonnet-4.5 │ │
│ │ Detect auth scheme│ │  │   .exchange_token(    │  │ └───────────────────┘ │
│ │ from AgentCard    │ │  │     user_token,       │  │                       │
│ │                   │ │  │     "mcp-gateway",    │  │ Shared checkpointer   │
│ │ OIDC → exchange   │ │  │     ["openid",...]    │  │ for all graphs        │
│ │ JWT  → client creds│ │  │   )                   │  │                       │
│ └───────────────────┘ │  └───────────────────────┘  └───────────────────────┘
└───────────────────────┘                                        │
        │                                                        │
        │                                                        ▼
        │                              ┌─────────────────────────────────────────┐
        │                              │ Graph Execution with Runtime Context    │
        │                              │ ┌─────────────────────────────────────┐ │
        │                              │ │ graph.astream(                      │ │
        │                              │ │   input_data,                       │ │
        │                              │ │   config={"thread_id": context_id}, │ │
        │                              │ │   context=GraphRuntimeContext(      │ │
        │                              │ │     tool_registry={...},  # Per-user│ │
        │                              │ │     subagent_registry={...},        │ │
        │                              │ │     language="en", ...              │ │
        │                              │ │   )                                 │ │
        │                              │ │ )                                   │ │
        │                              │ └─────────────────────────────────────┘ │
        │                              └─────────────────────────────────────────┘
        │                                                        │
        │                                                        ▼
        │                              ┌─────────────────────────────────────────┐
        │                              │ DynamicToolDispatchMiddleware           │
        │                              │ ┌─────────────────────────────────────┐ │
        │                              │ │ 1. Intercepts model calls           │ │
        │                              │ │ 2. Binds tools from runtime context │ │
        │                              │ │    context.tool_registry            │ │
        │                              │ │ 3. Dispatches tool calls            │ │
        │                              │ └─────────────────────────────────────┘ │
        │                              └─────────────────────────────────────────┘
        │                                                        │
        │                                                        ▼
        │                              ┌─────────────────────────────────────────┐
        │                              │ DynamoDB Checkpoint                     │
        │                              │ ┌─────────────────────────────────────┐ │
        │                              │ │ Stored: messages, state, interrupts │ │
        │                              │ │ NOT stored: tokens, tools, subagents│ │
        │                              │ │ TTL: configurable via AgentSettings │ │
        │                              │ └─────────────────────────────────────┘ │
        │                              └─────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ Sub-Agent Call (via A2AClientRunnable)                                         │
│ ┌───────────────────────────────────────────────────────────────────────────┐ │
│ │ SmartTokenInterceptor.intercept()                                         │ │
│ │                                                                           │ │
│ │ IF AgentCard.security_schemes has openIdConnect:                          │ │
│ │   → Token Exchange (RFC 8693)                                             │ │
│ │   exchanged = oauth2_client.exchange_token(                               │ │
│ │     subject_token=user_token,                                             │ │
│ │     target_client_id="subagent-client-id",                                │ │
│ │     requested_scopes=["openid", "profile"]                                │ │
│ │   )                                                                       │ │
│ │   headers["Authorization"] = f"Bearer {exchanged}"                        │ │
│ │                                                                           │ │
│ │ ELIF AgentCard.security_schemes has http/bearer/JWT:                      │ │
│ │   → Client Credentials Flow                                               │ │
│ │   token = oauth2_client.get_token(audience="subagent-client-id")          │ │
│ │   headers["Authorization"] = f"Bearer {token}"                            │ │
│ │   metadata["user_context"] = {user_id, email, name}  # Attribution        │ │
│ │                                                                           │ │
│ │ ELSE:                                                                     │ │
│ │   → No authentication                                                     │ │
│ └───────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## Limitations for Multi-Human Scenarios

### Current Architecture Assumptions

The current architecture is designed for **single-user conversations**:

1. **Graph Caching by Model Type**
   - Graphs are cached by model type (`gpt4o` or `claude-sonnet-4.5`), not by user or capability
   - Multiple users share graph instances, customized via `GraphRuntimeContext`
   - User isolation relies on `thread_id` in checkpointer

2. **Dynamic Tool Injection via Runtime Context**
   - Tools and sub-agents are discovered per-request based on user's token
   - `DynamicToolDispatchMiddleware` binds tools from `GraphRuntimeContext.tool_registry`
   - Each user gets their authorized tools at runtime, not at graph construction

3. **Single User Token per Request**
   - Only one `user_token` flows through the system per request
   - Token exchange produces tokens for a single user's context
   - User context is extracted from a single OIDC token

4. **Checkpoint Structure**
   - `thread_id` = `context_id` (conversation ID)
   - State is keyed by conversation, not by user
   - No multi-user state management in checkpoints

### Challenges for Multi-Human Agent Scenarios

If the orchestrator needed to handle conversations with **multiple human participants** in the same conversation thread (e.g., collaborative workflows, approval chains, handoffs between humans):

#### 1. **Per-Request Credential Injection (What Already Works)**

The current architecture already handles many multi-user scenarios correctly:

```python
# Each request brings its own token
user_config = UserConfig(user_id=user_id, access_token=user_token, ...)

# Discovery uses THAT user's permissions
user_config = await self.discover_capabilities(user_config)  # User B's token → User B's capabilities

# Graph selected by model type (shared), tools from runtime context (per-user)
graph = await self.get_or_create_graph(user_config.model)

# Convert to GraphRuntimeContext for runtime injection
runtime_context = self.build_runtime_context(user_config)

# Sub-agent calls use User B's token via SmartTokenInterceptor
```

**This means:**
- ✅ User B gets tools/subagents matching their permissions
- ✅ Sub-agent calls use User B's token
- ✅ MCP tools use User B's exchanged token
- ✅ No credential leakage between users
- ✅ Same graph instance, different runtime context

#### 2. **Checkpoint Continuity (Semantic Confusion Risk)**

**Problem:** Conversation history is shared across all participants in a `context_id`.

**Impact:**
- If User A's messages reference tools User B doesn't have, the LLM may try to call unavailable tools
- "I approved it" from User B appears in context when User A asked the question
- No technical errors (different tools via `GraphRuntimeContext`), but **semantic coherence** breaks down

**Example:**
```
# Checkpoint contains:
User A: "Create a Jira ticket for this bug"
AI: "I'll use the JiraAgent to create the ticket..."

# User B (no Jira access) continues the conversation:
# - LLM sees history mentioning JiraAgent
# - User B's GraphRuntimeContext doesn't have JiraAgent in tool_registry
# - DynamicToolDispatchMiddleware won't bind JiraAgent for User B
# - LLM may hallucinate or fail gracefully depending on prompt
```

#### 3. **Interrupt Ownership**

**Problem:** If User A's request triggers an interrupt (e.g., `auth_required`), the system doesn't track who "owns" that interrupt.

```python
# User A triggers auth_required interrupt
yield AgentStreamResponse.auth_required(message="Please authenticate with Jira")

# User B resumes the conversation
# - User B may not have the same auth context
# - User B may not even need the same authentication
# - The interrupt was meant for User A
```

#### 4. **Discovery Overhead**

**Problem:** Tools and sub-agents are re-discovered on every request.

```python
# Current: Discovery on every request
if user_config.tools is None or user_config.sub_agents is None:
    user_config = await self.discover_capabilities(user_config)
runtime_context = self.build_runtime_context(user_config)
```

**Impact:**
- Adds latency to each request
- No per-user capability caching
- Could be optimized with TTL-based per-user cache
```

#### 5. **Summary: Multi-Party Support Status**

**What works correctly:**
- Each request uses the requesting user's credentials (by design)
- Different users can participate in the same `context_id` across requests
- Graph selection and sub-agent auth use the current user's permissions
- Session JWTs work correctly (browser = one user; API clients cache per-user)

**What may cause issues:**
- Semantic coherence when users have different capabilities (see section 2)
- Interrupt ownership not tracked (see section 3)
- Discovery overhead on every request (see section 4)

### Recommended Changes for Multi-Human Support

For true multi-party conversation support:

1. **Participant Tracking in Checkpoints**
   ```python
   # Track who said what in the conversation
   state["participant_history"] = [
       {"user_id": "user_a", "message_ids": [1, 3, 5]},
       {"user_id": "user_b", "message_ids": [2, 4]},
   ]
   ```

2. **Interrupt Ownership**
   ```python
   # Tag interrupts with the user who triggered them
   interrupt_value = {
       "task_state": TaskState.auth_required,
       "owner_user_id": user_id,  # Track who needs to resolve this
       "message": "Please authenticate with Jira",
   }
   ```

3. **Per-User Capability Caching**
   ```python
   class UserCapabilityCache:
       cache: Dict[str, CachedCapabilities]  # user_id → capabilities
       ttl: int = 300  # 5 minutes
       
       async def get_or_discover(self, user_id: str, token: str) -> Capabilities:
           if user_id in self.cache and not self.cache[user_id].expired:
               return self.cache[user_id].capabilities
           # Discover and cache
   ```

4. **Conversation Scope Awareness**
   ```python
   # System prompt could include participant context
   system_prompt += f"""
   This conversation has multiple participants:
   - {user_a_name}: Started the conversation, has access to Jira
   - {user_b_name}: Approver, has access to Confluence only
   
   Current speaker: {current_user_name}
   """
   ```

### Migration Path

1. **Phase 1:** Add participant tracking to checkpoint state (backward compatible)
2. **Phase 2:** Implement per-user capability caching with TTL
3. **Phase 3:** Add interrupt ownership tracking
4. **Phase 4:** Enhance system prompt with participant context
5. **Phase 5:** Handle explicit handoff scenarios between participants

---

## Summary

| Component | Purpose | Credential Handling |
|-----------|---------|---------------------|
| `OidcUserinfoMiddleware` | Validate OIDC tokens | Caches in session JWT (15 min) |
| `UserContextFromRequestStateMiddleware` | Transfer to ContextVar | In-memory per request |
| `AuthRequestContextBuilder` | Build A2A RequestContext | Passes to call_context.state |
| `OrchestratorDeepAgentExecutor` | Extract & use credentials | Creates UserConfig |
| `GraphFactory` | Create/cache graphs by model type | No credentials stored |
| `DynamicToolDispatchMiddleware` | Bind tools at runtime | Uses GraphRuntimeContext |
| `SmartTokenInterceptor` | Sub-agent auth | Token exchange or client creds |
| `OidcOAuth2Client` | Token operations | Per-audience caching with expiry |
| `DynamoDBSaver` | Checkpoint state | NO credentials stored |

**Key Security Properties:**
- ✅ Zero-trust: Identity from validated JWT only
- ✅ No credentials in checkpoints
- ✅ Service-specific tokens via RFC 8693 exchange
- ✅ Session JWT caching reduces OIDC calls
- ✅ Token expiry checking with leeway
- ✅ Per-request credential injection handles multi-user capability differences
- ✅ Dynamic tool binding via `GraphRuntimeContext` (not baked into graphs)
- ⚠️ Multi-party conversations work but may have semantic coherence issues
