# Subagent Flow Architecture

This document describes the complete end-to-end flow of how subagents are discovered, registered, invoked, and how A2A protocol metadata (context_id/task_id) is managed for multi-turn conversations.

## Table of Contents

1. [Overview](#overview)
2. [Architecture Components](#architecture-components)
3. [Middleware Stack](#middleware-stack)
4. [Subagent Types](#subagent-types)
5. [Request Flow](#request-flow)
6. [One Live Task Per Sub-Agent](#one-live-task-per-sub-agent)
7. [A2A Protocol & Context ID Management](#a2a-protocol--context-id-management)
8. [Sequence Diagrams](#sequence-diagrams)

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

### 1. Runtime Parameters: config vs context

**LangGraph invocations use TWO distinct parameters:**

#### `config` (RunnableConfig) - Execution Control
Standard LangGraph parameter for infrastructure/observability:
- **Checkpoint isolation**: `configurable.thread_id` and `configurable.checkpoint_ns`
- **Cost tracking**: `tags` for LangSmith attribution
- **Metadata**: `user_id`, `assistant_id` for tracking
- **Callbacks**: LangChain handler propagation

#### `context` (GraphRuntimeContext) - Runtime Data
Custom parameter (enabled by `context_schema=GraphRuntimeContext`) for user-specific data:
- **Tool registry**: User's MCP tools
- **SubAgent registry**: Available sub-agents
- **User preferences**: name, language, custom prompt
- **File attachments**: Ephemeral content blocks

**Both are required and serve different purposes:**
```python
result = await graph.ainvoke(
    {"messages": [...]},
    config=config,        # Infrastructure (checkpointing, tracking)
    context=context,      # Runtime data (tools, user info)
)
```

### 2. GraphRuntimeContext

Per-user context passed at invocation time containing:

```python
class GraphRuntimeContext(BaseModel):
    user_id: str
    tool_registry: Dict[str, BaseTool]      # MCP tools discovered at runtime
    subagent_registry: Dict[str, CompiledSubAgent]  # All subagents (local + remote)
    a2a_tracking: Dict[str, Dict[str, Any]]  # Per-subagent tracking state
    # ... plus user preferences, file attachments, etc.
```

### 3. CompiledSubAgent

Wrapper around subagent runnables stored in the registry:

```python
CompiledSubAgent = TypedDict("CompiledSubAgent", {
    "name": str,
    "description": str,
    "runnable": BaseA2ARunnable,  # The actual executable
})
```

### 4. BaseA2ARunnable

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

## One Live Task Per Sub-Agent

A sub-agent's memory is its LangGraph checkpoint, and that checkpoint is addressed by
conversation and agent name alone:

```python
# DynamicToolDispatchMiddleware._adispatch_task_tool
_effective_thread_id = f"{orchestrator_conversation_id}::{subagent_type}"
# ...mirroring agents/dynamic_agent.py::get_thread_id -> f"{context_id}::dynamic-{name}"
```

Two `task` calls to the **same** `subagent_type` in one assistant message therefore run
on **one thread**. Their writes interleave, the last writer wins, and the loser's
conversation is gone. Observed: a "who am I on GitHub" delegation resumed on the
campaign-listing delegation's state and answered about ad campaigns — the GitHub tool
was never called again, and the authorization it had parked on was never answered.

**The second concurrent call to one agent is refused** (`surplus_same_agent_call` →
`_concurrent_same_agent_refusal`, in both `wrap_tool_call` and `awrap_tool_call`).
Different agents still run in parallel. The model is told the rule up front in the
task tool description (`_ONE_TASK_PER_AGENT_GUIDANCE`), so it folds same-agent work
into a single task itself; the refusal is the backstop.

Only for agents in `subagent_registry` — the ones whose thread this middleware owns.
A name outside it falls through to `SubAgentMiddleware`, which runs its sub-agent
inline against the parent's config with no `{conv}::{agent}` thread of its own
(and the built-in general-purpose does not use A2A tracking at all), so there is
nothing shared to corrupt; and an unknown or typo'd name must reach deepagents'
*"does not exist, the only allowed types are […]"*, which teaches the model the real
names, rather than being told a non-existent agent is busy and must never be
reported as unavailable. In practice this costs nothing: the orchestrator registers
its own `general-purpose` (see `AGENTS.md`), and the task tool's `subagent_type`
enum is built from the registry.

Ownership among siblings is decided by **position**, not by comparing ids:
`ToolCall["id"]` is optional, and comparing a possibly-`None` owner id would return
"allowed" for every sibling — disabling the guard exactly when the history is
malformed. An id-less owner still owns.

### The refusal is not a delegation result

It travels as a `task` ToolMessage and lands **after** the owner's (parallel siblings
are written in `tool_calls` order), so every consumer that reads "the latest `task`
result" would read it instead of the real answer. It carries
`additional_kwargs["concurrent_task_refusal"]` and those consumers skip it
(`app/middleware/task_refusal.py`):

| Consumer | Untagged consequence |
|----------|----------------------|
| `StreamHandler.parse_agent_response` (`include_subagent_output`) | the user's whole visible reply becomes *"This call was NOT executed…"* and the real answer is dropped |
| `StreamHandler._extract_recently_called_subagents` | a turn whose only `task` output is a refusal counts as a delegation, skipping the executor's re-entry nudge |
| `A2ATaskTrackingMiddleware.before_model` | the owner's `a2a_metadata` is never read, so a parked `input-required`/`auth-required` owner loses the `task_id` needed to resume it |

The refusal's wording must also never say a task *"does not exist"* — that phrase is
the stale-task heuristic in `a2a_tracking.py`, which would delete the **owner's** live
`task_id`. Pinned by a test.

### Why not just isolate the threads?

Because separate threads make two parked tasks *distinguishable* but still not
*addressable*, and two layers downstream need to address them:

| Layer | With two parked tasks for one agent |
|-------|-------------------------------------|
| Sub-agent checkpoint / interrupt id | collide — fixable by isolation |
| Client → orchestrator auth answer | `authorizationDataPart` sends a verdict with no interrupt id, so one "Done, continue" answers **both** prompts — including one whose card the user never saw |
| Model → orchestrator continuation | nothing can name which parked task a follow-up continues: `TaskToolSchema` (description, subagent_type) belongs to deepagents |

The last two each need a contract change — in every client, and in a library we do not
own. One live task per agent removes the need for either: there is never a second
candidate to address.

### What still works

- **Different agents in parallel** — untouched, including two that both need authorization.
- **Sequential re-delegation** — a later `task` call with a new `tool_call_id` continues
  the agent's thread, which is how a sub-agent remembers earlier delegations and how a
  parked `input-required`/`auth-required` task is resumed (`a2a_tracking` keeps
  `context_id` and clears `task_id` only once the task completes).

The guard reads only the assistant message that issued the call, which LangGraph replays
unchanged, so the sibling that won the first attempt wins the resume replay too — a
refusal cannot become a second execution part way through a turn.

### What this does NOT cover

The invariant is enforced *within one assistant message*. Other routes to the same
thread remain open, and each needs a claim on the thread itself (the
`StreamCoordinator.try_register/release` pattern in
`ringier-a2a-sdk/server/executor.py` is the shape that would subsume all of them):

- **Two orchestrator turns on one conversation** — a second user message arriving
  mid-turn, or a scheduled run landing on the same `context_id`. Each sees a lone
  sibling.
- **A stall-timeout abort** — the consumer is cancelled, but a remote A2A sub-agent
  keeps executing on `{conv}::{agent}` while the model is told the task failed and
  may retry.
- **Re-delegation into a parked task** — the refusal advises "wait for the running
  task's result and delegate the remainder afterwards". If the owner is still parked
  when that follow-up lands, the pre-call pending-interrupt probe turns the dispatch
  into `Command(resume=…)`, which *replaces* the freshly built `HumanMessage`: the
  follow-up's description is silently dropped and the old task resumes instead. Safe
  in the common case (the parked owner is resumed first, in the same turn), but not
  by construction.

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
