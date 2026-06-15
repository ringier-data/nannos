# Orchestrator Agent Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Tech Stack

- FastAPI + A2A protocol for agent communication
- LangGraph for orchestration state machine
- deepagents SDK (v0.5.7+) for graph primitives and sub-agent dispatch
- DynamoDB + S3 for checkpoints
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
- Has `ToolsetSelectorMiddleware` for LLM-driven smart tool filtering per task
- Is the **primary executor of skills** — when the orchestrator is unsure which sub-agent to use, it delegates to GP
- Loaded from DB as a user-configured sub-agent (name `"general-purpose"`)
- Uses the same `DynamicLocalAgentRunnable` code path as other local agents

### Sub-Agent Registry & Tool Registry

Built dynamically at runtime in `build_runtime_context()`:

- **tool_registry**: `{name: BaseTool}` — all discovered MCP tools + document store tools + catalog tools
- **subagent_registry**: `{name: CompiledSubAgent}` — file-analyzer, task-scheduler, remote A2A agents, dynamic local agents, GP agent
- Built-in sub-agents: `file-analyzer` (system), `task-scheduler` (system)
- Dynamic local sub-agents from user configuration (loaded from DB)
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

### Error Classification for Sub-Agent Failures

`ErrorClassificationMiddleware` classifies errors from sub-agent execution (auth failures, tool errors, etc.) to provide actionable feedback to the orchestrator's planning loop.

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
