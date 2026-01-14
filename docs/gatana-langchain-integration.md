---
title: LangChain Integration
description: Integrate Gatana MCP Gateway with LangChain agents for dynamic tool discovery
---

# Integrating Gatana with LangChain

This guide shows how to connect LangChain agents to Gatana MCP Gateway for dynamic tool discovery and execution.

## Overview

Gatana serves as a centralized MCP (Model Context Protocol) gateway that exposes tools to LangChain agents. The integration uses:

- **langchain-mcp-adapters** - Python library bridging MCP protocol with LangChain tools
- **MultiServerMCPClient** - LangChain MCP adapter for server connections
- **StreamableHttpConnection** - HTTP transport with SSE support

## Installation

```bash
pip install langchain-mcp-adapters
```

## Quick Start

### Basic Connection

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

async def get_gatana_tools():
    client = MultiServerMCPClient(
        connections={
            "gatana": StreamableHttpConnection(
                transport="streamable_http",
                url="https://your-gatana-instance.com/mcp",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        }
    )
    
    tools = await client.get_tools()
    return tools
```

### Using Tools with LangChain Agent

```python
from langchain.agents import create_agent

# Discover tools from Gatana
tools = await get_gatana_tools()

# Create agent with discovered tools
agent = create_agent(
    model,
    system_prompt="You are a helpful assistant.",
    tools=tools,
)

# Invoke the agent
result = await agent.ainvoke({"messages": [HumanMessage(content="Hello")]})
```

## Authentication

Gatana supports OAuth2 token exchange (RFC 8693) for multi-tenant authentication. This allows you to exchange a user's access token for a Gatana-specific token.

### Token Exchange Flow

```
User Request ──► Your Service ──► OIDC Provider ──► Gatana MCP Gateway
     │                │                │                    │
     │  Access Token  │                │                    │
     ├───────────────►│                │                    │
     │                │  Token Exchange│                    │
     │                ├───────────────►│                    │
     │                │  Gatana Token  │                    │
     │                │◄───────────────┤                    │
     │                │         MCP Request (Bearer Token)  │
     │                ├────────────────────────────────────►│
     │                │         Tools / Response            │
     │                │◄────────────────────────────────────┤
```

### Token Exchange Implementation

```python
from authlib.integrations.httpx_client import AsyncOAuth2Client

async def exchange_token_for_gatana(
    subject_token: str,
    client_id: str,
    client_secret: str,
    issuer: str,
) -> str:
    """Exchange user token for Gatana-specific token (RFC 8693)."""
    
    token_endpoint = f"{issuer}/protocol/openid-connect/token"
    
    async with AsyncOAuth2Client(
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint=token_endpoint,
    ) as client:
        token = await client.fetch_token(
            url=token_endpoint,
            grant_type="urn:ietf:params:oauth:grant-type:token-exchange",
            subject_token=subject_token,
            subject_token_type="urn:ietf:params:oauth:token-type:access_token",
            requested_token_type="urn:ietf:params:oauth:token-type:access_token",
            audience="mcp-gateway",
            scope="openid profile offline_access",
        )
        return token["access_token"]
```

### Full Example with Authentication

```python
async def discover_tools_with_auth(user_token: str) -> list:
    # Exchange token for Gatana access
    gatana_token = await exchange_token_for_gatana(
        subject_token=user_token,
        client_id="your-client-id",
        client_secret="your-client-secret",
        issuer="https://your-oidc-provider.com/realms/your-realm",
    )
    
    # Connect with exchanged token
    client = MultiServerMCPClient(
        connections={
            "gatana": StreamableHttpConnection(
                transport="streamable_http",
                url="https://your-gatana-instance.com/mcp",
                headers={"Authorization": f"Bearer {gatana_token}"},
            )
        }
    )
    
    return await client.get_tools()
```

## Tool Discovery Patterns

### Lazy Loading

Defer tool discovery until the agent is actually invoked to reduce startup time:

```python
class LazyToolAgent:
    def __init__(self, gatana_url: str, token: str):
        self.gatana_url = gatana_url
        self.token = token
        self._tools = None
        self._agent = None
    
    async def _ensure_tools(self):
        if self._tools is None:
            client = MultiServerMCPClient(
                connections={
                    "gatana": StreamableHttpConnection(
                        transport="streamable_http",
                        url=self.gatana_url,
                        headers={"Authorization": f"Bearer {self.token}"},
                    )
                }
            )
            self._tools = await client.get_tools()
        return self._tools
    
    async def invoke(self, message: str):
        tools = await self._ensure_tools()
        
        if self._agent is None:
            self._agent = create_agent(model, tools=tools)
        
        return await self._agent.ainvoke({"messages": [HumanMessage(content=message)]})
```

### Tool Whitelisting

Filter discovered tools to only use specific ones:

```python
async def get_filtered_tools(allowed_tools: list[str]) -> list:
    client = MultiServerMCPClient(
        connections={
            "gatana": StreamableHttpConnection(
                transport="streamable_http",
                url="https://your-gatana-instance.com/mcp",
                headers={"Authorization": f"Bearer {token}"},
            )
        }
    )
    
    all_tools = await client.get_tools()
    
    # Filter to only allowed tools
    return [tool for tool in all_tools if tool.name in allowed_tools]
```

## Tool Schema Validation

MCP tools may have schemas that are incompatible with OpenAI's API requirements. OpenAI requires that `parameters` fields include a `properties` object, even if empty.

### Validation Function

```python
from pydantic import create_model
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

def validate_tool_schema(tool: BaseTool) -> BaseTool:
    """Validate and fix MCP tool schema for OpenAI API compatibility."""
    
    # Check if tool has args_schema
    if not hasattr(tool, "args_schema") or tool.args_schema is None:
        tool.args_schema = create_model(f"{tool.name}Args")
        return tool
    
    # Verify the schema converts correctly
    tool_dict = convert_to_openai_tool(tool)
    parameters = tool_dict.get("function", {}).get("parameters")
    
    # Fix invalid schemas
    if parameters is None or not isinstance(parameters, dict) or "properties" not in parameters:
        tool.args_schema = create_model(f"{tool.name}Args")
    
    return tool

# Apply to discovered tools
tools = await client.get_tools()
validated_tools = [validate_tool_schema(tool) for tool in tools]
```

> **Warning:** Invalid tool schemas cause OpenAI to return 400 errors before streaming begins. This breaks SSE responses and causes unexpected JSON responses instead of text/event-stream.

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GATANA_MCP_URL` | Gatana MCP gateway URL |
| `OIDC_ISSUER` | OIDC provider URL for token exchange |
| `OIDC_CLIENT_ID` | OAuth2 client ID |
| `OIDC_CLIENT_SECRET` | OAuth2 client secret |

### Example Configuration

```python
import os

GATANA_CONFIG = {
    "url": os.getenv("GATANA_MCP_URL", "https://your-gatana-instance.com/mcp"),
    "oidc_issuer": os.getenv("OIDC_ISSUER"),
    "client_id": os.getenv("OIDC_CLIENT_ID"),
    "client_secret": os.getenv("OIDC_CLIENT_SECRET"),
}
```

## MCP JSON-RPC Direct Access

For scenarios where you need direct MCP protocol access without LangChain adapters:

```python
import httpx

async def list_tools_jsonrpc(gatana_url: str, token: str) -> dict:
    """List tools using MCP JSON-RPC protocol directly."""
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            gatana_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
        )
        
        # Handle SSE or JSON response
        if "text/event-stream" in response.headers.get("content-type", ""):
            # Parse SSE response
            for line in response.text.split("\n"):
                if line.startswith("data:"):
                    return json.loads(line[5:])
        
        return response.json()
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Your Application                        │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐    ┌─────────────────────┐         │
│  │   LangChain Agent   │    │  Tool Discovery     │         │
│  │                     │◄───│  Service            │         │
│  └──────────┬──────────┘    └──────────┬──────────┘         │
│             │                          │                    │
│             │    ┌─────────────────────┘                    │
│             │    │                                          │
│             ▼    ▼                                          │
│  ┌─────────────────────────────────────┐                    │
│  │      MultiServerMCPClient           │                    │
│  │    + StreamableHttpConnection       │                    │
│  └──────────────────┬──────────────────┘                    │
│                     │                                       │
└─────────────────────┼───────────────────────────────────────┘
                      │ HTTPS + Bearer Token
                      ▼
          ┌───────────────────────────┐
          │    Gatana MCP Gateway     │
          │    (JSON-RPC + SSE)       │
          └───────────────────────────┘
```

## Best Practices

### 1. Zero-Trust Authentication

Never persist credentials in checkpoints or state. Use token exchange for each downstream call:

```python
# Good: Exchange token per-request
gatana_token = await exchange_token(user_token, "mcp-gateway")

# Bad: Storing tokens in persistent state
state["gatana_token"] = token  # Don't do this
```

### 2. Lazy Tool Discovery

Discover tools only when needed to reduce startup latency and resource consumption.

### 3. Tool Whitelisting

Specify exactly which tools an agent needs to reduce attack surface and improve LLM focus.

### 4. Schema Validation

Always validate MCP tool schemas before using with OpenAI-based models to prevent streaming errors.

### 5. Error Handling

```python
async def safe_discover_tools() -> list:
    try:
        client = MultiServerMCPClient(...)
        tools = await client.get_tools()
        return [validate_tool_schema(tool) for tool in tools]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise AuthenticationError("Invalid or expired token")
        raise
    except Exception as e:
        logger.error(f"Tool discovery failed: {e}")
        return []  # Graceful degradation
```

## Troubleshooting

### 400 Errors from OpenAI

**Symptom:** OpenAI returns 400 before streaming begins when using MCP tools.

**Cause:** Invalid tool schema missing `properties` field.

**Solution:** Apply `validate_tool_schema()` to all discovered tools.

### SSE Parsing Issues

**Symptom:** Unexpected JSON response instead of SSE stream.

**Cause:** Missing `Accept: text/event-stream` header or error before streaming.

**Solution:** Include proper Accept header and validate tool schemas.

### Token Exchange Failures

**Symptom:** 401 or 403 from Gatana after token exchange.

**Cause:** OIDC client not configured for token exchange or missing audience.

**Solution:** Ensure your OIDC client has token exchange permissions and `mcp-gateway` audience is configured.
