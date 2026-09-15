# Subagent Flow Architecture

This document describes the complete end-to-end flow of how subagents are discovered, registered, invoked, and how the A2A task lifecycle (context_id/task_id, pauses and resumes) is managed for multi-turn conversations.

## Table of Contents

1. [Overview](#overview)
2. [Architecture Components](#architecture-components)
3. [Middleware Stack](#middleware-stack)
4. [Subagent Types](#subagent-types)
5. [Request Flow](#request-flow)
6. [Every Delegation Is an A2A Task](#every-delegation-is-an-a2a-task)
7. [A2A Protocol & Context ID Management](#a2a-protocol--context-id-management)
8. [Sequence Diagrams](#sequence-diagrams)

---

## Overview

The orchestrator uses a **middleware-based architecture** to handle subagent invocations. This design enables:

- **Single graph instance** serving all users with different subagent configurations
- **Dynamic tool/subagent injection** at runtime without graph recreation
- **A2A protocol compliance** for multi-turn conversation continuity — for remote sub-agents over HTTP *and* for local sub-agents, which run behind an in-process A2A server (`agent_common.a2a.local_server`, ADR-0008)
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

Abstract base class for all subagent implementations. Both concrete families stream the
same typed events — `TaskUpdate` (carrying a `TaskResponseData`: the A2A task's own
`task_id`/`context_id`/`state`, plain text in `messages[-1].content`, the server's extras in
`metadata`), `ArtifactUpdate` (streamed content) and `ErrorEvent` — so the dispatch does not
know, and does not need to know, which transport produced them:

| Runnable | Transport | Where the task lifecycle lives |
|----------|-----------|--------------------------------|
| `A2AClientRunnable` | HTTP (A2A SDK client) | the remote server |
| `LocalA2ARunnable` (`DynamicLocalAgentRunnable`, `FileAnalyzerRunnable`, `FoundryLocalAgentRunnable`, test mocks) | in-process (`runnable.local_server`: A2A SDK `DefaultRequestHandler` + `TaskStore` + `LocalSubAgentExecutor`) | the sub-agent's own in-process server |

A local runnable exposes two entry points:

- **`astream(input, config)`** — the *client* side: builds an A2A message from the `SubAgentInput`, sends it to the in-process server, translates the events (`agent_common.a2a.event_translation`). This is what the dispatch calls.
- **`astream_graph(input | Command, config)`** — the *graph* side: instruments the run and drives `_astream_impl`. This is what the server's executor calls, and what the orchestrator's embedded execute-only path (`OrchestratorDeepAgent.stream_subagent`) drives directly, because that path *is* the A2A server for its sub-agent.

---

## Middleware Stack

Middleware executes in this order (defined in `graph_factory.py`):

```
DynamicToolDispatch → UserPreferences → AuthError → ToolRetry → A2ATaskTracking → TodoStatus
```

### Middleware Responsibilities

| Middleware | Hook | Responsibility |
|------------|------|----------------|
| **DynamicToolDispatchMiddleware** | `wrap_model_call`, `wrap_tool_call` | Inject dynamic tools/subagents; dispatch `task` calls as A2A tasks; park the turn on a local sub-agent's structured interrupt |
| **UserPreferencesMiddleware** | `wrap_model_call` | Inject user language preferences into system prompt |
| **AuthErrorDetectionMiddleware** | `wrap_tool_call` | Detect and handle auth errors from orchestrator tools |
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

User-configured agents with custom system prompts:

```python
subagent_registry["data-analyst"] = CompiledSubAgent(
    name="data-analyst",
    description="Analyzes data...",
    runnable=DynamicLocalAgentRunnable(system_prompt="You are a data expert..."),
)
```

Their graph runs as a standalone LangGraph root on the thread `{context_id}::dynamic-{name}`
(`agent_common.a2a.threads.local_sub_agent_thread_id`) — the same thread agent-runner executes a
scheduled run of the same agent on, which is what lets a conversation adopt a run by context id.

### 3. Remote A2A Subagents

External agents accessed via A2A protocol over HTTP:

```python
subagent_registry["jira-agent"] = CompiledSubAgent(
    name="jira-agent",
    description="Manages Jira tickets",
    runnable=A2AClientRunnable(agent_card=card),
)
```

### 4. General-Purpose Subagent (Special Case)

The orchestrator registers its own `general-purpose` (a `DynamicLocalAgentRunnable`, see `AGENTS.md`).
A `subagent_type` that is **not** in `subagent_registry` falls through to deepagents'
`SubAgentMiddleware`, which runs its built-in inline against the parent's config — no A2A task,
no tracking.

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
│  1. User record (local_subagents, sub_agents configs)                   │
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
│  DynamicToolDispatchMiddleware.awrap_tool_call()                         │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  1. Is tool_name == "task"?                                        │  │
│  │  2. Is subagent_type in user_context.subagent_registry?            │  │
│  │     - YES → _adispatch_task_tool()                                 │  │
│  │     - NO  → return None (fall through to SubAgentMiddleware)       │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ _adispatch_task_tool():                                                  │
│ 1. Build the SubAgentInput: HumanMessage (+ filtered files), the state's │
│    a2a_tracking, orchestrator_conversation_id, and                        │
│    proposed_task_id = delegation_task_id(conversation, tool_call_id)     │
│ 2. LOCAL agent only: tasks/get(proposed id). Parked on a question?       │
│    → this is a REPLAY: interrupt() returns the user's answer; the input   │
│      becomes the answer, addressed to that task                          │
│ 3. runnable.astream(input, config) — one A2A exchange                    │
│ 4. Final TaskUpdate: terminal, or input_required / auth_required         │
│    LOCAL agent with raw interrupts in metadata → interrupt(value):        │
│      first pass raises (turn parks), replay returns the answer → step 3  │
│ 5. ToolMessage(content=text, additional_kwargs.a2a_metadata={ids,state}) │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Every Delegation Is an A2A Task

A `task` tool call opens exactly one A2A task on the sub-agent, and the task is named after
the call:

```python
task_id = delegation_task_id(orchestrator_conversation_id, tool_call_id)   # uuid5, stable across replay
subagent_state["proposed_task_id"] = task_id
```

A2A servers normally mint task ids. The orchestrator proposes this one
(`SubAgentInput.proposed_task_id`, honoured by `ProposedTaskIdContextBuilder` when no task by
that id exists yet) because a delegation is a LangGraph tool call, and LangGraph **replays that
call byte-identical** when the orchestrator resumes from an interrupt. If the first attempt
parked the sub-agent on a question, the replay must find *that* task and deliver the user's
answer to it — not open a second task and run the work twice. `tasks/get` on the proposed id is
therefore the replay detector; the sub-agent's checkpoint is never probed by the orchestrator.

Continuity is still `a2a_tracking`'s call: a live `task_id` recorded there means the message
**continues** that task (the agent asked the user something and this is the reply); the proposal
only applies to a message that opens a task.

### Pausing and resuming a local sub-agent

A local sub-agent's graph can park on a LangGraph interrupt — a risk-gated tool approval
(`ConditionalHumanInTheLoopMiddleware`), an in-task authorization, a client-action round trip.
The graph runs as a standalone root, so the interrupt is suppressed into its checkpoint; the
runnable re-raises it after the stream and its in-process executor turns it into the task's
state:

| Interrupt kind | Task state | Status message |
|----------------|------------|----------------|
| `action_requests` (tool approval) | `input_required` | human-in-the-loop extension: TextPart + `{action_requests, review_configs}` DataPart |
| `task_state == AUTH_REQUIRED` | `auth_required` | in-task-auth extension: TextPart + `AuthPayload.client_payload()` DataPart |
| `client_action_request` | `input_required` | client-action extension: `{request}` DataPart |
| anything else | `input_required` | TextPart (+ the raw value as DataPart) |

The raw interrupts ride the status **event's** metadata (`interrupts: [{id, value}]`). The
dispatch reads them off the final `TaskUpdate` and calls the orchestrator's own `interrupt()`
with the first value, so the client gets the structured approval card exactly as before. On the
replay the answer travels back to the task as a **message** — a `decisions` / `authorization`
DataPart, or the user's own words as text (`answer_to_human_message`) — and the sub-agent's
executor fits it to the interrupt it is parked on (`local_server.resume.build_resume_command`:
id-keyed map, blanket-decision replication, auth-vs-approval alignment) and resumes the graph.

A question the sub-agent asks **in words**, through its structured response
(`task_state: input_required`), carries no interrupts: it reaches the model as an ordinary result
and the model relays it; the user's reply continues the same task on the next delegation.

### Same agent, several tasks

A sub-agent's memory is one checkpoint thread per (conversation, agent). Two `task` calls to the
same agent in one assistant message are two A2A tasks on that one thread; the in-process
executor **serialises** graph runs per conversation (`LocalA2AServer.thread_lock`), so the second
runs as a follow-up on the agent's conversation and both results reach the model. The only
refusal left is server-side and A2A-native: a *new* task arriving while the thread is parked on a
question is `rejected` with an explanation (`PARKED_TASK_MESSAGE`) — its description is work,
not an answer, and delivering it to the interrupt reader would reject the pending call as "not
an answer". The parked task still resumes normally.

### What this does NOT cover

The lock lives in the orchestrator process. Other routes to the same thread remain open, and
each needs a claim on the thread itself (the `StreamCoordinator.try_register/release` pattern in
`ringier-a2a-sdk/server/executor.py` is the shape that would subsume all of them):

- **Two orchestrator turns on one conversation** — a second user message arriving mid-turn,
  or a scheduled run landing on the same `context_id` from agent-runner.
- **A stall-timeout abort** — the consumer is cancelled, but a remote A2A sub-agent keeps
  executing on its thread while the model is told the task failed and may retry.

---

## A2A Protocol & Context ID Management

### The Two Paths for Context ID

| Path | Used By | Mechanism |
|------|---------|-----------|
| **State Path** | Registry subagents (local and remote) | `a2a_tracking` passed in `subagent_state`; `_extract_tracking_ids` waterfall |
| **Fallback** | first call to an agent | `orchestrator_conversation_id` becomes the A2A `context_id` |

```python
# BaseA2ARunnable._extract_tracking_ids():
agent_tracking = input_data.a2a_tracking.get(self.tracking_key, {})
context_id = agent_tracking.get("context_id") or input_data.orchestrator_conversation_id
task_id = agent_tracking.get("task_id")        # only while the task is not complete
```

For a local agent the in-process executor stamps the task's own ids into the graph's
`a2a_tracking` record, so `_astream_impl` sees the A2A task as its own.

### Response Flow: Typed, Not Enveloped

A sub-agent's result is a `TaskResponseData`, whichever transport produced it:

```python
TaskResponseData(
    task_id="…", context_id="…",
    state=TaskState.TASK_STATE_COMPLETED,          # protobuf enum value
    messages=[AIMessage(content="The actual response text")],
    metadata={...},                                 # the server's extras (auth details, session handles, raw interrupts)
)
```

`DynamicToolDispatchMiddleware._extract_subagent_response` reads the text from the last
message and builds `a2a_metadata` from the typed fields (`state` as the enum *name*, e.g.
`TASK_STATE_COMPLETED`) plus the server's metadata minus the raw interrupts, and puts it in
`ToolMessage.additional_kwargs["a2a_metadata"]`. There is no JSON in the message text.

### State Persistence: before_model

```python
# A2ATaskTrackingMiddleware.before_model() runs at START of each iteration

# 1. Every ToolMessage the step just wrote (parallel delegations return in one step)
# 2. a2a_metadata from additional_kwargs, keyed by the issuing call's subagent_type
# 3. task_id kept only while the task is not complete; context_id always kept
# 4. Return {"a2a_tracking": ...} for LangGraph to merge
```

---

## Sequence Diagrams

### First Turn: New Conversation

```
User                LLM              DynamicToolDispatch     Subagent (server)   A2ATracking
  │                  │                       │                   │                   │
  │─────────────────▶│                       │                   │                   │
  │  "Create JIRA"   │                       │                   │                   │
  │                  │──task(jira-agent)────▶│                   │                   │
  │                  │                       │──message/stream──▶│  opens task T     │
  │                  │                       │  (proposed id T)  │  (context = conv) │
  │                  │                       │◀──status events───│                   │
  │                  │                       │  … completed(T)   │                   │
  │                  │◀──ToolMessage─────────│                   │                   │
  │                  │  (a2a_metadata)       │                   │                   │
  │                  │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │──before_model()──▶│
  │◀─────────────────│                       │                   │  record ids       │
  │  "Created JIRA-123"                      │                   │                   │
```

### A Local Sub-Agent Needs an Approval

```
User            Orchestrator graph      DynamicToolDispatch     Local server        Sub-agent graph
  │                    │                        │                    │                    │
  │──"who am I"───────▶│──task(github)─────────▶│──message(T)───────▶│──astream_graph────▶│
  │                    │                        │                    │◀──GraphInterrupt───│ parked
  │                    │                        │◀──input_required(T)│  (checkpointed)    │
  │                    │                        │   + interrupts     │                    │
  │                    │◀──interrupt(value)─────│  raises            │                    │
  │◀──approval card────│  turn parks            │                    │                    │
  │                    │                        │                    │                    │
  │──approve──────────▶│  Command(resume)       │                    │                    │
  │                    │──replay task(github)──▶│──tasks/get(T)─────▶│ parked             │
  │                    │                        │  interrupt() → ans │                    │
  │                    │                        │──message(T, ans)──▶│──Command(resume)──▶│
  │                    │                        │◀──completed(T)─────│◀──result───────────│
  │                    │◀──ToolMessage──────────│                    │                    │
  │◀──"you are …"──────│                        │                    │                    │
```

### General-Purpose Fallback (Not in the Registry)

A `subagent_type` that is not in `subagent_registry` returns `None` from the dispatch and falls
through to deepagents' `SubAgentMiddleware`: no A2A task, no `a2a_metadata`, nothing for
`before_model` to persist.

---

## Summary

| Aspect | Registry sub-agents (local & remote) | Fallback (not in registry) |
|--------|--------------------------------------|----------------------------|
| **Dispatched by** | DynamicToolDispatchMiddleware, as an A2A task | SubAgentMiddleware (deepagents) |
| **Task lifecycle** | the sub-agent's server (HTTP or in-process) | N/A |
| **Replay after an interrupt** | `tasks/get(delegation_task_id)` finds the parked task | N/A |
| **A2A Tracking** | Yes (multi-turn) | No |
| **Response shape** | typed `TaskResponseData` | plain text |
| **State update** | A2ATaskTrackingMiddleware.before_model | N/A |
