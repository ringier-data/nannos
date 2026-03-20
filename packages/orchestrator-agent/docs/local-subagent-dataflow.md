# Local Sub-Agent Data Flow

This document explains how dynamic local sub-agents are provisioned and executed within the orchestrator.

## Overview

Local sub-agents are user-specific agents that run **in-process** (not over the network like A2A remote agents) but still communicate using the A2A protocol. They enable users to configure personal assistants with custom system prompts and optional dedicated MCP tool servers.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                   DynamoDB                                       │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  Users Table                                                               │  │
│  │  ┌─────────────────────────────────────────────────────────────────────┐  │  │
│  │  │  user_id: "user-123"                                                │  │  │
│  │  │  local_subagents: [                                                 │  │  │
│  │  │    {                                                                │  │  │
│  │  │      "name": "data-analyst",                                        │  │  │
│  │  │      "description": "Analyzes data and generates insights",         │  │  │
│  │  │      "system_prompt": "You are a data analysis expert...",          │  │  │
│  │  │      "mcp_gateway_url": null  ← inherit orchestrator tools          │  │  │
│  │  │    },                                                               │  │  │
│  │  │    {                                                                │  │  │
│  │  │      "name": "jira-helper",                                         │  │  │
│  │  │      "description": "Manages Jira tickets",                         │  │  │
│  │  │      "system_prompt": "You are a Jira expert...",                   │  │  │
│  │  │      "mcp_gateway_url": "https://jira-mcp.example.com"  ← override  │  │  │
│  │  │    }                                                                │  │  │
│  │  │  ]                                                                  │  │  │
│  │  └─────────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        │ 1. Discover capabilities
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              OrchestratorDeepAgent                               │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  discover_capabilities()                                                  │  │
│  │  ┌─────────────────────────────────────────────────────────────────────┐  │  │
│  │  │  • Reads User record from DynamoDB                                  │  │  │
│  │  │  • Discovers remote A2A sub-agents                                  │  │  │
│  │  │  • Discovers MCP tools                                              │  │  │
│  │  │  • Returns enriched UserConfig with:                                │  │  │
│  │  │    - local_subagents: list[LocalSubAgentConfig]                     │  │  │
│  │  │    - tools: list[BaseTool] (from MCP discovery)                     │  │  │
│  │  │    - sub_agents: list[dict] (from A2A discovery)                    │  │  │
│  │  └─────────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                        │                                         │
│                                        │ 2. Build runtime context                │
│                                        ▼                                         │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  build_runtime_context(user_config, llm_model, oauth2_client)             │  │
│  │  ┌─────────────────────────────────────────────────────────────────────┐  │  │
│  │  │  For each local_subagent config:                                    │  │  │
│  │  │    1. Validate with LocalSubAgentConfig (Pydantic)                  │  │  │
│  │  │    2. Create DynamicLocalAgentRunnable                              │  │  │
│  │  │    3. Wrap in CompiledSubAgent                                      │  │  │
│  │  │    4. Register in subagent_registry                                 │  │  │
│  │  └─────────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                        │                                         │
│                                        │ 3. GraphRuntimeContext                  │
│                                        ▼                                         │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  subagent_registry: dict[str, CompiledSubAgent]                           │  │
│  │  ┌─────────────────────────────────────────────────────────────────────┐  │  │
│  │  │  "data-analyst" → CompiledSubAgent(DynamicLocalAgentRunnable)       │  │  │
│  │  │  "jira-helper"  → CompiledSubAgent(DynamicLocalAgentRunnable)       │  │  │
│  │  │  "FileAnalyzer" → CompiledSubAgent(FileAnalyzerRunnable)            │  │  │
│  │  │  "RemoteAgent"  → CompiledSubAgent(A2AClientRunnable)               │  │  │
│  │  └─────────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## Execution Flow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              User Request                                        │
│                    "Analyze the sales data in the CSV file"                      │
└─────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Orchestrator LLM Decision                              │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  LLM sees available sub-agents in system prompt:                          │  │
│  │  - data-analyst: "Analyzes data and generates insights"                   │  │
│  │  - jira-helper: "Manages Jira tickets"                                    │  │
│  │                                                                           │  │
│  │  LLM decides: Call task(subagent_type="data-analyst", description="...")  │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        │ 4. task() tool invoked
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        DynamicLocalAgentRunnable                                 │
│                           (First Invocation - Lazy Init)                         │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  _ensure_agent()                                                          │  │
│  │  ┌─────────────────────────────────────────────────────────────────────┐  │  │
│  │  │  IF mcp_gateway_url is set:                                         │  │  │
│  │  │    → _discover_mcp_tools() from gateway (OVERRIDE)                  │  │  │
│  │  │  ELSE:                                                              │  │  │
│  │  │    → Use orchestrator_tools (INHERIT)                               │  │  │
│  │  │                                                                     │  │  │
│  │  │  Create LangGraph agent with:                                       │  │  │
│  │  │    - Custom system_prompt + A2A protocol addendum                   │  │  │
│  │  │    - Discovered/inherited tools                                     │  │  │
│  │  │    - SubAgentResponseSchema for structured output                   │  │  │
│  │  └─────────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                        │                                         │
│                                        │ 5. Agent processes request              │
│                                        ▼                                         │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  Inner LangGraph Agent Execution                                          │  │
│  │  ┌─────────────────────────────────────────────────────────────────────┐  │  │
│  │  │  • Uses custom system prompt                                        │  │  │
│  │  │  • Has access to tools (inherited or discovered)                    │  │  │
│  │  │  • Executes multi-step reasoning                                    │  │  │
│  │  │  • MUST output SubAgentResponseSchema with:                         │  │  │
│  │  │    - task_state: "completed" | "input_required" | "failed"          │  │  │
│  │  │    - message: Response content                                      │  │  │
│  │  └─────────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                        │                                         │
│                                        │ 6. Structured response                  │
│                                        ▼                                         │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  _translate_agent_result()                                                │  │
│  │  ┌─────────────────────────────────────────────────────────────────────┐  │  │
│  │  │  Extract SubAgentResponseSchema:                                    │  │  │
│  │  │    - OpenAI: From structured_response                               │  │  │
│  │  │    - Bedrock: From tool_calls[SubAgentResponseSchema]               │  │  │
│  │  │                                                                     │  │  │
│  │  │  Map to A2A response:                                               │  │  │
│  │  │    completed      → _build_success_response()                       │  │  │
│  │  │    input_required → _build_input_required_response()                │  │  │
│  │  │    failed         → _build_error_response()                         │  │  │
│  │  └─────────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        │ 7. A2A-compliant response
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Back to Orchestrator                                   │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  Response: {                                                              │  │
│  │    "messages": [AIMessage(...)],                                          │  │
│  │    "state": "completed" | "input_required" | "failed",                    │  │
│  │    "is_complete": true | false,                                           │  │
│  │    "requires_input": false | true,                                        │  │
│  │    "task_id": "...",                                                      │  │
│  │    "context_id": "..."                                                    │  │
│  │  }                                                                        │  │
│  │                                                                           │  │
│  │  Orchestrator LLM decides next action:                                    │  │
│  │    - completed: Report result to user                                     │  │
│  │    - input_required: Ask user for clarification                           │  │
│  │    - failed: Handle error or try alternative                              │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## Tool Inheritance vs Override

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          Tool Resolution Strategy                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  CASE 1: mcp_gateway_url = null (INHERIT)                                        │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │                                                                           │  │
│  │   Orchestrator                    Local Sub-Agent                         │  │
│  │   ┌─────────────┐                ┌─────────────────┐                      │  │
│  │   │ MCP Tools:  │───────────────▶│ Uses same tools │                      │  │
│  │   │ - tool_a    │   (shared)     │ - tool_a        │                      │  │
│  │   │ - tool_b    │                │ - tool_b        │                      │  │
│  │   │ - tool_c    │                │ - tool_c        │                      │  │
│  │   └─────────────┘                └─────────────────┘                      │  │
│  │                                                                           │  │
│  │   Use case: Sub-agent needs same capabilities as orchestrator             │  │
│  │   Example: Data analyst using the same database query tools               │  │
│  │                                                                           │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  CASE 2: mcp_gateway_url = "https://..." (OVERRIDE)                              │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │                                                                           │  │
│  │   Orchestrator                    Local Sub-Agent                         │  │
│  │   ┌─────────────┐                ┌─────────────────┐                      │  │
│  │   │ MCP Tools:  │      ✗        │ Dedicated tools │                      │  │
│  │   │ - tool_a    │  (ignored)    │ - jira_create   │◀──┐                  │  │
│  │   │ - tool_b    │                │ - jira_search   │   │                  │  │
│  │   │ - tool_c    │                │ - jira_update   │   │                  │  │
│  │   └─────────────┘                └─────────────────┘   │                  │  │
│  │                                         ▲              │                  │  │
│  │                                         │              │                  │  │
│  │                               ┌─────────┴──────────┐   │                  │  │
│  │                               │ MCP Gateway        │   │                  │  │
│  │                               │ jira-mcp.example   │───┘                  │  │
│  │                               │ (lazy discovery)   │                      │  │
│  │                               └────────────────────┘                      │  │
│  │                                                                           │  │
│  │   Use case: Sub-agent needs specialized tools not available to orch      │  │
│  │   Example: Jira helper with dedicated Jira MCP server                     │  │
│  │                                                                           │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## Structured Output: Why No Guessing

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    SubAgentResponseSchema (Explicit State)                       │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  BEFORE (Pattern Matching - Unreliable):                                         │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  Agent says: "Please provide the project name"                            │  │
│  │              ↓                                                            │  │
│  │  Heuristic: contains "please provide" → input_required  ✓                 │  │
│  │                                                                           │  │
│  │  Agent says: "I can provide you with the analysis"                        │  │
│  │              ↓                                                            │  │
│  │  Heuristic: contains "provide" → input_required  ✗ WRONG!                 │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  AFTER (Structured Output - Reliable):                                           │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  Agent outputs:                                                           │  │
│  │  {                                                                        │  │
│  │    "task_state": "input_required",  ← LLM explicitly decides              │  │
│  │    "message": "Please provide the project name"                           │  │
│  │  }                                                                        │  │
│  │                                                                           │  │
│  │  Agent outputs:                                                           │  │
│  │  {                                                                        │  │
│  │    "task_state": "completed",  ← LLM explicitly decides                   │  │
│  │    "message": "I can provide you with the analysis: ..."                  │  │
│  │  }                                                                        │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  Implementation:                                                                 │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  OpenAI models:                                                           │  │
│  │    response_format=AutoStrategy(schema=SubAgentResponseSchema)            │  │
│  │                                                                           │  │
│  │  Bedrock models:                                                          │  │
│  │    tools.append(SubAgentResponseSchema as StructuredTool)                 │  │
│  │    System prompt: "You MUST call SubAgentResponseSchema..."               │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `LocalSubAgentConfig` | `app/subagents/models.py` | Pydantic model for config validation |
| `DynamicLocalAgentRunnable` | `app/subagents/dynamic_agent.py` | Main runnable with lazy agent creation |
| `SubAgentResponseSchema` | `app/subagents/dynamic_agent.py` | Structured output for task state |
| `create_dynamic_local_subagent` | `app/subagents/dynamic_agent.py` | Factory function |
| `UserConfig.to_runtime_context` | `app/models/config.py` | Creates runnables from config |
| `User.local_subagents` | `app/core/registry.py` | DynamoDB field for user config |

## Benefits

1. **User-Specific**: Each user can configure their own sub-agents
2. **No Deployment**: Runs in-process, no separate A2A server needed
3. **Lazy Initialization**: Agent only created when first called
4. **Tool Flexibility**: Inherit orchestrator tools or use dedicated MCP server
5. **A2A Compliant**: Uses standard A2A protocol for orchestrator communication
6. **Explicit State**: LLM determines task state via structured output, no guessing
