# Subagent Flow Architecture

This document describes the complete end-to-end flow of how subagents are discovered, registered, invoked, and how A2A protocol metadata (context_id/task_id) is managed for multi-turn conversations.

## Table of Contents

1. [Overview](#overview)
2. [Architecture Components](#architecture-components)
3. [Middleware Stack](#middleware-stack)
4. [Subagent Types](#subagent-types)
5. [Request Flow](#request-flow)
6. [A2A Protocol & Context ID Management](#a2a-protocol--context-id-management)
7. [Sequence Diagrams](#sequence-diagrams)

---

## Overview

The orchestrator uses a **middleware-based architecture** to handle subagent invocations. This design enables:

- **Single graph instance** serving all users with different subagent configurations
- **Dynamic tool/subagent injection** at runtime without graph recreation
- **A2A protocol compliance** for multi-turn conversation continuity
- **Transparent context_id/task_id management** without LLM involvement

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Orchestrator Agent                              │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                         Middleware Stack                                │ │
│  │  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────────┐   │ │
│  │  │DynamicToolDispatch│→│  UserPreferences │→│AuthErrorDetection  │   │ │
│  │  └──────────────────┘  └──────────────────┘  └────────────────────┘   │ │
│  │           ↓                                                            │ │
│  │  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────────┐   │ │
│  │  │   ToolRetry      │→│ A2ATaskTracking  │→│   TodoStatus       │   │ │
│  │  └──────────────────┘  └──────────────────┘  └────────────────────┘   │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                     │                                        │
│                                     ▼                                        │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                    GraphRuntimeContext (per-user)                       │ │
│  │  ┌─────────────────────┐  ┌─────────────────────────────────────────┐  │ │
│  │  │    tool_registry    │  │           subagent_registry             │  │ │
│  │  │  (MCP tools)        │  │  - file-analyzer (local)                │  │ │
│  │  │                     │  │  - data-analyst (local dynamic)         │  │ │
│  │  │                     │  │  - jira-agent (remote A2A)              │  │ │
│  │  └─────────────────────┘  └─────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Architecture Components

### 1. GraphRuntimeContext

Per-user context passed at invocation time containing:

```python
class GraphRuntimeContext(BaseModel):
    user_id: str
    tool_registry: Dict[str, BaseTool]      # MCP tools discovered at runtime
    subagent_registry: Dict[str, CompiledSubAgent]  # All subagents (local + remote)
    a2a_tracking: Dict[str, Dict[str, Any]]  # Per-subagent tracking state
```

### 2. CompiledSubAgent

Wrapper around subagent runnables stored in the registry:

```python
CompiledSubAgent = TypedDict("CompiledSubAgent", {
    "name": str,
    "description": str,
    "runnable": BaseA2ARunnable,  # The actual executable
})
```

### 3. BaseA2ARunnable

Abstract base class for all subagent implementations:

```python
class BaseA2ARunnable(ABC):
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    async def ainvoke(self, input_data: Dict) -> Dict: ...
```

---

## Middleware Stack

Middleware executes in this order (defined in `graph_factory.py`):

```
DynamicToolDispatch → UserPreferences → AuthError → ToolRetry → A2ATaskTracking → TodoStatus
```

### Middleware Responsibilities

| Middleware | Hook | Responsibility |
|------------|------|----------------|
| **DynamicToolDispatchMiddleware** | `wrap_model_call`, `wrap_tool_call` | Inject dynamic tools/subagents; dispatch to subagent_registry |
| **UserPreferencesMiddleware** | `wrap_model_call` | Inject user language preferences into system prompt |
| **AuthErrorDetectionMiddleware** | `wrap_tool_call` | Detect and handle auth errors from subagents |
| **ToolRetryMiddleware** | `wrap_tool_call` | Retry failed tool calls |
| **A2ATaskTrackingMiddleware** | `before_model` | Extract and persist context_id/task_id to state |
| **TodoStatusMiddleware** | `before_model` | Track todo list state |

---

## Subagent Types

### 1. Local Built-in Subagents

Hard-coded agents like `file-analyzer` that are always available:

```python
# Registered at build_runtime_context()
subagent_registry["FileAnalyzer"] = CompiledSubAgent(
    name="FileAnalyzer",
    description="Analyzes files...",
    runnable=FileAnalyzerRunnable(),
)
```

### 2. Local Dynamic Subagents

User-configured agents defined in DynamoDB with custom system prompts:

```python
# From user's local_subagents config in DynamoDB
subagent_registry["data-analyst"] = CompiledSubAgent(
    name="data-analyst",
    description="Analyzes data...",
    runnable=DynamicLocalAgentRunnable(system_prompt="You are a data expert..."),
)
```

### 3. Remote A2A Subagents

External agents accessed via A2A protocol over HTTP:

```python
# Discovered from user's sub_agents list
subagent_registry["jira-agent"] = CompiledSubAgent(
    name="jira-agent",
    description="Manages Jira tickets",
    runnable=A2AClientRunnable(url="https://jira-a2a.example.com"),
)
```

### 4. General-Purpose Subagent (Special Case)

The `general-purpose` subagent is **NOT** in `subagent_registry`. It's handled specially:

- **DynamicToolDispatchMiddleware** returns `None` when subagent_type not found
- Request falls through to **SubAgentMiddleware** handler (from deepagents library)
- **general-purpose does NOT use A2A tracking** - it's a stateless ephemeral agent

---

## Request Flow

### Phase 1: Discovery & Registration

```
┌──────────────────┐     ┌─────────────────────┐     ┌───────────────────┐
│  HTTP Request    │────▶│ OrchestratorAgent   │────▶│ discover_         │
│  with user_id    │     │ .handle_request()   │     │ capabilities()    │
└──────────────────┘     └─────────────────────┘     └───────────────────┘
                                                              │
         ┌────────────────────────────────────────────────────┘
         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Discover:                                                               │
│  1. User record from DynamoDB (local_subagents, sub_agents configs)     │
│  2. Remote A2A agents via A2A discovery protocol                        │
│  3. MCP tools from user's MCP gateway                                   │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  build_runtime_context():                                                │
│  - Create DynamicLocalAgentRunnable for each local_subagent config      │
│  - Create A2AClientRunnable for each remote sub_agent                   │
│  - Create FileAnalyzerRunnable (built-in)                               │
│  - Register all in GraphRuntimeContext.subagent_registry                │
└─────────────────────────────────────────────────────────────────────────┘
```

### Phase 2: Tool Invocation

```
┌──────────────────────────────────────────────────────────────────────────┐
│  LLM decides to call: task(subagent_type="jira-agent", description="...") │
└──────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  DynamicToolDispatchMiddleware.wrap_tool_call()                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  1. Check: Is tool_name == "task"?                                 │  │
│  │  2. Check: Is subagent_type in user_context.subagent_registry?     │  │
│  │     - YES → Dispatch directly via _dispatch_task_tool()            │  │
│  │     - NO  → Return None (fall through to next middleware)          │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                                      │
                    ┌─────────────────┴─────────────────┐
                    │                                   │
            Found in registry                    Not found (general-purpose)
                    │                                   │
                    ▼                                   ▼
┌─────────────────────────────────┐   ┌─────────────────────────────────────┐
│ _dispatch_task_tool():          │   │ A2ATaskTrackingMiddleware           │
│ 1. Get runnable from registry   │   │ .awrap_tool_call():                 │
│ 2. Prepare subagent_state:      │   │ 1. Inject context_id/task_id        │
│    - Include a2a_tracking       │   │ 2. Call handler() →                 │
│    - Set messages=[HumanMsg]    │   │    SubAgentMiddleware               │
│ 3. runnable.invoke(state)       │   │ 3. Unwrap response metadata         │
│ 4. Unwrap JSON response         │   └─────────────────────────────────────┘
│ 5. Return ToolMessage           │
└─────────────────────────────────┘
```

---

## A2A Protocol & Context ID Management

### The Two Paths for Context ID

There are **two mechanisms** for passing context_id to subagents:

| Path | Used By | Mechanism |
|------|---------|-----------|
| **State Path** | Dynamic subagents (in registry) | `a2a_tracking` passed in `subagent_state` |
| **Args Injection** | general-purpose | `awrap_tool_call` injects into tool args |

### State Path (Dynamic Subagents)

```python
# In DynamicToolDispatchMiddleware._dispatch_task_tool():

# 1. Prepare state including a2a_tracking
excluded_keys = ("messages", "todos")
subagent_state = {k: v for k, v in state.items() if k not in excluded_keys}
subagent_state["messages"] = [HumanMessage(content=description)]

# 2. Subagent extracts via _extract_tracking_ids()
# In BaseA2ARunnable._extract_tracking_ids():
agent_tracking = input_data.a2a_tracking.get(self.name, {})
context_id = agent_tracking.get("context_id")
task_id = agent_tracking.get("task_id")
```

### Response Flow: Extracting Metadata

All subagents wrap their response content as JSON:

```json
{
  "content": "The actual response text",
  "a2a": {
    "task_id": "uuid-1234",
    "context_id": "uuid-5678",
    "state": "completed",
    "is_complete": true,
    "requires_input": false,
    "requires_auth": false
  }
}
```

**Unwrapping happens in DynamicToolDispatchMiddleware:**

- **DynamicToolDispatchMiddleware._dispatch_task_tool()** - For all subagents in `subagent_registry`
  - Parses JSON content
  - Extracts `a2a` metadata
  - Puts metadata in `ToolMessage.additional_kwargs["a2a_metadata"]`
  - Returns clean content to LLM

**Note:** The `general-purpose` subagent (from deepagents library) is **stateless** and does NOT use A2A tracking.
It falls through to SubAgentMiddleware's handler and doesn't produce `a2a_metadata`.

### State Persistence: before_model

```python
# A2ATaskTrackingMiddleware.before_model() runs at START of each iteration

# 1. Find ToolMessage from previous iteration
last_message = messages[-1]

# 2. Extract a2a_metadata from additional_kwargs
a2a_metadata = last_message.additional_kwargs.get("a2a_metadata")

# 3. Update a2a_tracking state
current_tracking[subagent_type]["context_id"] = a2a_metadata["context_id"]
current_tracking[subagent_type]["task_id"] = a2a_metadata["task_id"]

# 4. Return state update for LangGraph to merge
return {"a2a_tracking": current_tracking}
```

---

## Sequence Diagrams

### First Turn: New Conversation

```
User                LLM              DynamicToolDispatch     Subagent          A2ATracking
  │                  │                       │                   │                   │
  │─────────────────▶│                       │                   │                   │
  │  "Create JIRA"   │                       │                   │                   │
  │                  │                       │                   │                   │
  │                  │──task(jira-agent)────▶│                   │                   │
  │                  │                       │                   │                   │
  │                  │                       │──invoke(state)───▶│                   │
  │                  │                       │  (no a2a_tracking)│                   │
  │                  │                       │                   │                   │
  │                  │                       │◀──JSON response───│                   │
  │                  │                       │  {content, a2a}   │                   │
  │                  │                       │                   │                   │
  │                  │◀──ToolMessage─────────│                   │                   │
  │                  │  (a2a in kwargs)      │                   │                   │
  │                  │                       │                   │                   │
  │                  │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │──before_model()──▶│
  │                  │                       │                   │  extract IDs      │
  │                  │                       │                   │  update state     │
  │                  │                       │                   │                   │
  │◀─────────────────│                       │                   │                   │
  │  "Created JIRA-123"                      │                   │                   │
```

### Second Turn: Continuing Conversation

```
User                LLM              DynamicToolDispatch     Subagent          A2ATracking
  │                  │                       │                   │                   │
  │─────────────────▶│                       │                   │                   │
  │  "Add comment"   │                       │                   │                   │
  │                  │                       │                   │                   │
  │                  │──task(jira-agent)────▶│                   │                   │
  │                  │                       │                   │                   │
  │                  │                       │──invoke(state)───▶│                   │
  │                  │                       │  a2a_tracking:    │                   │
  │                  │                       │   jira-agent:     │                   │
  │                  │                       │    context_id: X  │                   │
  │                  │                       │    task_id: Y     │                   │
  │                  │                       │                   │                   │
  │                  │                       │   Subagent calls: │                   │
  │                  │                       │   _extract_tracking_ids()             │
  │                  │                       │   Uses context_id X                   │
  │                  │                       │                   │                   │
  │                  │                       │◀──JSON response───│                   │
  │                  │                       │  (same context_id)│                   │
  │                  │                       │                   │                   │
  │                  │◀──ToolMessage─────────│                   │                   │
  │                  │                       │                   │                   │
  │◀─────────────────│                       │                   │                   │
  │  "Comment added" │                       │                   │                   │
```

### General-Purpose Flow (Stateless - No A2A Tracking)

The `general-purpose` subagent from the deepagents library is **stateless** and does NOT use
A2A tracking. It's designed for one-shot research tasks that don't need conversation continuity.

```
User                LLM              DynamicToolDispatch     SubAgentMiddleware
  │                  │                       │                   │
  │─────────────────▶│                       │                   │
  │  "Research X"    │                       │                   │
  │                  │                       │                   │
  │                  │──task(general-purpose)▶                   │
  │                  │                       │                   │
  │                  │                       │──Not in registry──│
  │                  │                       │  return None      │
  │                  │                       │                   │
  │                  │                       │──Falls through────▶
  │                  │                       │  to handler()     │
  │                  │                       │                   │
  │                  │                       │◀──result──────────│
  │                  │                       │  (no A2A metadata)│
  │                  │                       │                   │
  │                  │◀──ToolMessage─────────│                   │
  │                  │                       │                   │
  │◀─────────────────│                       │                   │
```

**Key Difference:** No `a2a_metadata` in the response, so `A2ATaskTrackingMiddleware.before_model`
has nothing to persist for `general-purpose` calls.

---

## Summary

| Aspect | Dynamic Subagents | General-Purpose |
|--------|-------------------|-----------------|
| **Registered in** | `subagent_registry` | SubAgentMiddleware (deepagents) |
| **Dispatched by** | DynamicToolDispatchMiddleware | SubAgentMiddleware (via handler fallback) |
| **A2A Tracking** | Yes (multi-turn) | **No** (stateless) |
| **Context ID source** | `state.a2a_tracking` | N/A |
| **Response unwrapping** | DynamicToolDispatchMiddleware | N/A (no JSON wrapping) |
| **State update** | A2ATaskTrackingMiddleware.before_model | N/A |

**Key Insight:** The `general-purpose` subagent is a **stateless** agent from the deepagents library.
It's designed for one-shot research tasks and does NOT participate in A2A tracking.

For dynamic subagents (local or remote A2A), all paths converge at `before_model` for state persistence,
ensuring consistent A2A tracking regardless of which middleware handled the actual dispatch.
