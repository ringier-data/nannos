# Orchestrator Agent Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Tech Stack

- FastAPI + A2A protocol for agent communication
- LangGraph for orchestration state machine
- deepagents SDK (v0.5.7+) for graph primitives and sub-agent dispatch
- PostgreSQL + optional S3 for checkpoints
- PostgreSQL + pgvector for document store (semantic indexing); the same database backs the A2A task store (`app/core/task_store.py`, in-memory fallback when Postgres is not configured)
- Pydantic v2 for data validation
- pytest with pytest-asyncio for testing

## Local Development Environment

**CRITICAL: Any changes that impact the local development environment MUST be reflected in the local start scripts.**

## Python Environment

This project uses `uv` for dependency management:

```bash
# Install dependencies
uv sync

# Run tests (prefer runTests MCP tool when available)
uv run pytest tests/ -v
```

## File Writing Safety

NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits with proper file writing tools instead.

## Architecture Patterns

### Single Graph Per Model, Dynamic Tool Injection

**CRITICAL**: The orchestrator uses ONE graph instance per model type, shared across ALL users. Tools are NOT baked into graphs — they are injected at runtime via `GraphRuntimeContext`.

- `DynamicToolDispatchMiddleware` merges tools from three sources at invocation time:
  - Original tools (write_todos, task/sub-agent dispatch)
  - Static tools (FinalResponseSchema for Bedrock)
  - User's dynamic MCP tools from `GraphRuntimeContext.tool_registry`
- This architecture enables horizontal scaling without per-user graph creation.

### General-Purpose (GP) Agent

The GP agent is a `DynamicLocalAgentRunnable` (from `agent-common`) registered as `"general-purpose"` in the subagent registry. It's special:

- Gets ALL tools from `tool_registry` via `inject_all_tools` (bypasses MCP gateway discovery)
- Is the **primary executor of skills** — when the orchestrator is unsure which sub-agent to use, it delegates to GP
- Loaded from DB as a user-configured sub-agent (name `"general-purpose"`)
- Uses the same `DynamicLocalAgentRunnable` code path as other local agents

**Tool filtering depends on PTC** (`CODE_INTERPRETER_PTC`):

- **PTC off (native tool calling):** `ToolsetSelectorMiddleware` is added — an LLM filters the full catalog down to a relevant per-turn subset, so hundreds of tools aren't bound to the model.
- **PTC on:** `ToolsetSelectorMiddleware` is **NOT** added. The catalog is exposed inside `eval` and the model discovers tools at runtime via `tools.search`/`tools.describe` (see `agent-common` → *PTC Tool Exposure*). This supersedes the selector (runtime discovery, no recall ceiling, no per-turn selection LLM call) and is required for prompt caching — keeping the selector under PTC would re-vary the exposed/rendered set per turn. The full catalog is still injected (`inject_all_tools`) so it can be exposed. Gating lives in `build_runtime_context()` via `code_interpreter_ptc_enabled()`.

### Sub-Agent Registry & Tool Registry

Built dynamically at runtime in `build_runtime_context()`:

- **tool_registry**: `{name: BaseTool}` — all discovered MCP tools + document store tools + catalog tools
- **subagent_registry**: `{name: CompiledSubAgent}` — file-analyzer, remote A2A agents, dynamic local agents (incl. task-scheduler), GP agent
- Built-in sub-agents: `file-analyzer` (system, code-instantiated)
- Dynamic local sub-agents from user configuration (loaded from DB) — includes the pre-seeded system agents `general-purpose`, `skill-assessor`, `agent-creator` and `task-scheduler`
- Remote A2A sub-agents from discovery

### HITL Guards for Skill Management

All self-improvement and skill management tools require user confirmation:

```python
HITL_GUARDED_TOOLS = {
    "console_create_bug_report": ["approve", "edit", "reject"],
    "console_create_skill": ["approve", "edit", "reject"],
    "console_update_skill": ["approve", "edit", "reject"],
    "console_remove_skill": ["approve", "edit", "reject"],
    "console_update_playbook": ["approve", "edit", "reject"],
    "console_import_skill": ["approve", "reject"],    # No "edit"
    "console_activate_skill": ["approve", "reject"],  # No "edit"
}
```

**Pattern**: Import and activate don't allow "edit" because there's nothing to edit (they take a registry ID, not content). Create/update/remove do allow edit because the user may want to modify what the LLM proposed.

### `agent_name` Default-Injection for Skill Tools

Skill management tools discovered from MCP include an `agent_name` parameter. The orchestrator wraps these tools via `_wrap_tool_with_agent_name()` to **default** `agent_name` to `"orchestrator"` when the LLM omits it. Unlike sub-agents (which hard-override and hide `agent_name`), the orchestrator keeps `agent_name` visible in the schema so the LLM can specify a different target sub-agent (e.g., for `console_activate_skill`).

```python
_SKILL_TOOLS_NEEDING_AGENT_NAME = {
    "console_create_skill", "console_update_skill", "console_remove_skill",
    "console_update_playbook", "console_write_skill_file", "console_delete_skill_file",
    "console_import_skill", "console_activate_skill",
}
```

Sub-agents use a hard-override + schema-stripping pattern (via `_wrap_with_agent_name()` in `dynamic_agent.py`) because they always operate on themselves.

### Sandbox Integration

The orchestrator passes a `SandboxPool` to `build_runtime_context()`, which propagates it to dynamic local sub-agents:

- SandboxPool is created once per orchestrator instance
- Each sub-agent with `sandbox_enabled=True` acquires a sandbox per A2A turn
- Sandboxes are keyed by `(session_id, sub_agent_name)` for warm reuse
- The GP agent typically does NOT use sandbox (it delegates to specialized agents)

### Playbook Injection Middleware

`PlaybookInjectionMiddleware` injects the orchestrator's AGENTS.md into the system prompt at runtime. The orchestrator itself does NOT have skills — it delegates task execution to sub-agents which each have their own `SkillsStoreBackend` with pre-resolved skills.

### Default Skills (core/default_skills.py)

The orchestrator ships with built-in default skills (e.g., `find-skills`). These are loaded into the graph's skill system and guide the orchestrator on how to discover, import, and activate skills for sub-agents.

## Critical Design Decisions

### One Graph Per Model Type, Not Per User

Graphs are cached by `(model_name, thinking_level)`. All users share the same compiled graph. User-specific state (tools, sub-agents, preferences) is injected at runtime via `GraphRuntimeContext` and `DynamicToolDispatchMiddleware`. This is critical for performance — graph compilation is expensive.

### GP Agent Replaces deepagents Built-In General-Purpose

The orchestrator overrides the deepagents SDK's built-in "general-purpose" agent with its own `DynamicLocalAgentRunnable` instance. This is done by registering it in `subagent_registry["general-purpose"]`. The custom GP agent has skill resolution, HITL-guarded self-improvement, and `ToolsetSelectorMiddleware` — none of which the built-in provides.

### Orchestrator Auto-Includes Scheduler + Console Tools

The orchestrator's whitelisted tools always include `scheduler_*` and `console_*` prefixed tools (auto-included regardless of user config). This ensures scheduling and skill management are always available without explicit user configuration.

### File-Analyzer Costs Attributed to Orchestrator

`file-analyzer` is created with `sub_agent_id=None`. This means its LLM costs are attributed to the orchestrator (not to any user-created sub-agent). This is intentional — it's a system capability.

### File-Analyzer Media Support & the Video Gap

`file-analyzer` (`app/agents/file_analyzer.py`) supports **images, PDFs, text, and audio**; **video is deliberately rejected** with a clear message (`_fetch_files`).

How each type reaches the model (all traffic goes through the gateway as a langchain `ChatOpenAI` client speaking OpenAI **Chat Completions** — ADR-0001):
- **Images** → `image_url` (a URL is valid for images in Chat Completions; LiteLLM fetches it for Bedrock).
- **PDFs** → fetched and **inlined as base64** (`file` block with `base64`). A `file` block carrying a *URL* is rejected at payload build ("file URLs … with Chat Completions"), and Bedrock/Vertex accept base64 document sources only — so base64 is the one portable form. Do **not** provider-gate this.
- **Text** → fetched inline as a text block.
- **Audio** → fetched and **inlined as base64** (same wire reason as PDFs — a URL `file` block is rejected). Kept because audio is a first-class chat input. **Capability-gated:** requires the resolved model to declare `audio` input (i.e. be audio-capable, e.g. Gemini — the fleet's cheap tier is `gemini-3.5-flash`); Claude has no audio modality. On a non-audio tier, audio is **rejected up front** with a clear message (`_reject_unsupported_media`) — *not* silently dropped to text (which read as "No processable files" and triggered pointless re-delegation to general-purpose). LiteLLM's Vertex path accepts base64 `file` blocks.

`get_supported_input_modes()` reflects this honestly: it narrows the model's declared modes to `_HANDLEABLE_MODES` — always drops `video`, and offers `audio`/`file` only when the model declares them — so the agent card and orchestrator routing don't over-promise. The **System Status** page has an "Audio transcription (file-analyzer)" row (`feature_status._audio_transcription_feature`) so an admin can see whether audio works and what to configure (an audio-capable model on the `chat:low`/`chat` default).
- **Video** → **rejected.** Model *capability* is no longer the blocker — the cheap tier is `gemini-3.5-flash`, which handles video. The blocker is **transport**: (1) a URL `file` block is rejected at payload build ("file URLs … with Chat Completions"), and (2) base64 doesn't scale to video (Gemini inline ~20 MB, request cap 32 MB). So neither form we can currently send works.

**Enabling video later — it's an upload pipeline, not a client tweak.** Vertex `fileData.file_uri` requires a `gs://` GCS URI or a **Gemini File API** handle; it will **not** fetch an arbitrary S3 presigned HTTPS URL (confirmed). Our attachments live in **S3**, so the real work is: (a) stage the video into a Gemini-reachable location — an S3→GCS copy (`gs://`) or a Gemini File API upload — which pulls **GCP credentials app-side** (the proxy holds Vertex creds, but the upload is orchestrator-side), a staging bucket + lifecycle cleanup, and File-API retention/size limits; (b) provider-aware model routing (video ⇒ Gemini); (c) emit the `file` block with the resulting URI + `format`/`video_metadata`. **Client choice for step (c):** patch `_GatewayChatOpenAI` to pre-rewrite the media block into the raw OpenAI `file` shape before the base translator runs — do **not** switch to `langchain-litellm`/`ChatLiteLLM` (a second client that bypasses the proxy and loses cost tracking, virtual keys, and the reasoning/`thinking_blocks`/`cache_control` handling). Bedrock video is limited to TwelveLabs Pegasus via a non-content-block `mediaSource` param the `ChatOpenAI` path can't express.

### Error Classification for Sub-Agent Failures

`ErrorClassificationMiddleware` classifies errors from sub-agent execution (auth failures, tool errors, etc.) to provide actionable feedback to the orchestrator's planning loop.

### Scheduled-Run Conversation Adoption: One Contract, Two Continuity Mechanisms

A conversation opened with a `scheduled_run` origin (conversation-origin extension) gets, besides the synthetic-history reconstruction, an `a2a_tracking` seed so the next delegation to the run's sub-agent **continues the run's own conversation** (`_validate_scheduled_run_origin` + `_build_adoption_seed` in `app/core/agent.py`). The contract is identical for both kinds; the record's continuity field differs by mechanism:

- **Remote agents** — `{"context_id": <run ctx>}`: agent-runner dispatched the run with the run task's contextId on the wire (`_run_remote_agent`), so the executing server checkpoints under exactly that id and the `A2AClientRunnable` waterfall resumes it.
- **Local/automated agents** — `{"adopt_thread_from": <run ctx>}`, **never `context_id`**: a raw context_id seed changes the runnable's execution thread while `DynamicToolDispatchMiddleware`'s HITL checkpoint probe still probes the `orchestrator_conversation_id`-derived thread, silently breaking tool-approval resume (the exact regression dropped in PR #161 round 1). Instead, the dispatch middleware **forks** the run's checkpoint (`_maybe_adopt_run_thread`): the run lives at bare `thread_id = run ctx` in the **shared** checkpoint tables (both services point at the same Postgres schema and both graphs come from `build_sub_agent_graph`); its latest checkpoint is copied once onto the conversation's own `{ctx}::dynamic-{name}` thread, first delegation only, blank target only. Fork, not re-point: the run's thread stays an untouched record, and parallel conversations adopting the same run don't share mutable state. Scheduled-run graphs carry no HITL middleware (agent-runner passes no `hitl_guarded_tools`/`risk_scorer` — scheduling is fail-open), so no pending interrupt can exist on the source, and `pending_writes` are never copied regardless; what a FAILED run can leave is a trailing `AIMessage` with unanswered tool_calls, which the fork seals with synthetic error `ToolMessage`s in the copy so the history stays provider-valid.
- **Foundry** — not adoptable; continuity is a `foundry_session_rid` the provenance doesn't carry.

Automated (scheduler-only) sub-agents carry `interactive=False` (`registry.py`) and are registered into a conversation **only** when it adopted one of their runs (`adopted_sub_agent_ids` through `build_runtime_context`) — for the rest of that conversation they behave like any local agent; everywhere else they stay invisible. The ids are validated server-side on the blank first turn, then **re-derived on every subsequent turn — HITL resumes included — from the `sub_agent_id` stamped into the persisted a2a_tracking adoption record** (`_adopted_sub_agent_ids_from_tracking`); deriving them from the origin DataPart alone would deregister the agent after turn one and drop the approval of its own first delegation's interrupt.

**Open alternative (unmeasured) — routing local/automated delegations to agent-runner as an A2A server.** agent-runner keys threads by bare contextId, so a context_id seed would resume a run natively (no fork, no shared-DB requirement) and adoption would collapse to the remote mechanism for every kind. The hypothesis against it — so far a hunch, not a measurement — is per-turn latency: a cold harness build on agent-runner's side (config fetch, token exchange, MCP discovery, graph compile) on top of the orchestrator's, versus in-process delegation reusing the warm per-user discovery cache, exchanged tokens, pooled checkpointer, gateway clients, and session-keyed sandboxes. Deciding it for real means benchmarking a delegation turn both ways (agent-runner warm-cache work included in the A2A variant). Interactive delegation via agent-runner would also need the HITL harness there first (scheduled runs are fail-open today). Until measured, in-process + fork-on-adopt stands.

Two cross-cutting invariants: the seed key is **`runnable.tracking_key`** (`agent_common/a2a/base.py`) — the single home of the name-with-spaces-stripped convention shared by the registry key (`discovery.py`), the `a2a_tracking` writers, and `_extract_tracking_ids`' reader; never re-derive it from a name. And the origin DataPart is untrusted: the job/run are re-resolved via console-backend under the authenticated user's token (single-run endpoint `GET /api/v1/scheduler/jobs/{id}/runs/{run_id}` — the run *listing* is capped to the newest 50), the job's server-side sub-agent binding must match, and the **server-stored** `conversation_id` is what gets seeded — the DataPart's `context_id` plays no part.

## Testing

**Prefer the runTests MCP tool over terminal commands when running tests.**

Fallback to direct pytest commands when needed:
```bash
uv run pytest tests/ -v
uv run pytest tests/test_specific.py -v
```

- Mock A2A transport for sub-agent communication tests
- Use real graph execution for middleware integration tests
- Test HITL interrupt flow end-to-end
- Verify `GraphRuntimeContext` construction for different user configs
