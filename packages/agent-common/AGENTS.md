# Agent Common Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Tech Stack

- Python library consumed by all agent services (orchestrator, agent-creator, agent-runner)
- LangGraph for agent execution graphs
- Pydantic v2 for data validation
- deepagents SDK (v0.5.7+) for graph building primitives
- pytest with pytest-asyncio for testing

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

### DynamicLocalAgentRunnable (agents/dynamic_agent.py)

The primary agent implementation for all user-configured sub-agents. Wraps a LangGraph agent with lazy initialization, MCP tool discovery, and A2A protocol compliance.

**Key design decisions:**

1. **Lazy initialization via `_ensure_agent()`**: Tools, skills, prompt, and graph are resolved only on first invocation (guarded by `_cached_tools` sentinel). After first call, it's a no-op.

2. **Console self-improvement tools are always injected** — independent of the agent's `mcp_tools` whitelist. Every sub-agent can create/update/remove skills and update playbooks via console-backend MCP. These are discovered via a separate `_discover_console_self_improvement_tools()` call.

3. **`agent_name` auto-injection**: All skill/playbook MCP tools have `agent_name` and `sub_agent_id` auto-injected via `_wrap_with_agent_name()`. The LLM never sees these parameters in the tool schema — they're hidden and injected at call time.

4. **Sandbox agents rebuild graph per invocation**: When `sandbox_enabled=True` and a `SandboxPool` is available, the graph is NOT built during `_ensure_agent()`. Instead, a fresh graph with sandboxed backends is built per `_astream_impl()` call. This is because each invocation needs a per-turn sandbox with its own filesystem.

5. **Post-stream interrupt detection**: After streaming completes, the code inspects `aget_state()` for suppressed interrupts (from `is_nested=False` graphs) and re-raises them as `GraphInterrupt`. This is necessary because standalone graphs suppress interrupts inside the Pregel loop.

6. **HITL-guarded tools**: All self-improvement tools require user approval via `HumanInTheLoopMiddleware`. The guard dict is defined in `_ensure_agent()` and passed to `build_sub_agent_graph()`.

### Self-Improvement Protocol

Sub-agents have a built-in self-improvement capability via MCP tools:

**Tools** (defined in `_CONSOLE_SELF_IMPROVEMENT_TOOLS`):
- `console_create_skill` — Creates skill in registry + auto-activates on calling agent
- `console_update_skill` — Updates registry entry + self-updates calling agent's activation
- `console_remove_skill` — Deactivates from calling agent (registry untouched)
- `console_activate_skill` — Activates existing registry skill on calling agent
- `console_update_playbook` — Updates AGENTS.md content
- `console_write_skill_file` / `console_delete_skill_file` — Manage bundled files
- `console_search_skills` / `console_import_skill` — Search and import external skills

**The self-improvement addendum** (`_build_self_improvement_addendum()`) is injected into the system prompt and acts as a decision tree guiding the agent on when to create skills vs. update playbooks. It adapts based on the agent's `effective_permission` level (owner/write/read).

### Three-Tier Skill Resolution (core/skills_resolver.py)

Skills are resolved once at agent initialization with override semantics:

```
personal > group > default
```

- **Default skills** come from the agent's config version (locked activations)
- **Group skills** are activated by group members (shared)
- **Personal skills** are activated by the individual user

A personal skill with the same name as a default/group skill overrides it completely. The resolved dict is passed to `SkillsStoreBackend` as an immutable snapshot.

### Attachments Store Backend (backends/attachments_store.py)

User-attached files are served at `/attachments/{filename}` so agents can `read_file`, `grep`, and `copy_to_sandbox` them. The backend is:

- **Ephemeral**: built per A2A turn from the message's typed `ContentBlock` list (`pending_file_blocks`) — nothing is persisted to PostgreSQL.
- **Lazy**: file bytes are fetched from the presigned URL only on first read and cached in memory for the lifetime of the backend instance (25 MB max per file).
- **Read-only**: writes/edits return a message pointing the agent to `copy_to_sandbox`.
- **Flat**: paths are always `/attachments/{filename}` — no sub-directories.

#### How filenames are derived (CRITICAL for path consistency)

`derive_attachment_filename(block, url, mime_type, idx, used_names)` is called by both the orchestrator and sub-agents and must produce the **same stable path** everywhere:

1. `block.get("filename") or block.get("name")` — preferred source. The orchestrator's `content_builder.py` injects `"filename": name` onto every `ContentBlock` dict so this always hits for named files.
2. `os.path.basename(urlparse(url).path)` — fallback when no explicit name. Produces an opaque UUID-like string from presigned S3 URLs, so **always set `"filename"` on the block**.
3. `f"attachment_{idx}{ext}"` — last-resort for unnamed blobs.

Path separators in the name are flattened to `_`. Duplicates get `_{idx}` appended.

#### Content block → `/attachments/` path contract

`_process_file_part()` in `orchestrator-agent/app/core/content_builder.py`:
1. Generates a presigned URL from the `s3://` URI (24 h expiry).
2. Calls `_describe_file()` which advertises `path=/attachments/{name}` in the LLM-visible text description.
3. Builds the typed `ContentBlock` and injects `content_block["filename"] = name` so `derive_attachment_filename` returns the same name the description advertised.

This three-step contract ensures the LLM can use `path=/attachments/report.pdf` directly without needing to `ls /attachments` first.

#### Mounting strategy: ContextVar vs. direct bake-in

The orchestrator's graph is **compiled once and shared across all users**, so it cannot bake a per-turn `AttachmentsStoreBackend` into its static `CompositeBackend`. Instead a single `ContextScopedAttachmentsBackend` is mounted at `/attachments/` — it is a stateless shell that delegates every call to whichever `AttachmentsStoreBackend` is stored in the `_current_attachments_backend` ContextVar for the running asyncio task.

Sub-agents build a **new graph per invocation** (or per sandbox turn), so they bake the concrete `AttachmentsStoreBackend` directly into their `invocation_backend_factory` — they never use the ContextVar.

| Consumer | Mount strategy |
|---|---|
| Orchestrator (shared graph) | `ContextScopedAttachmentsBackend` + ContextVar set/reset around each turn in `agent.py` |
| Sub-agents (per-user graph) | `AttachmentsStoreBackend` baked into `_compose_backend_with_attachments` in `dynamic_agent.py` |

`set_current_attachments_backend()` / `reset_current_attachments_backend()` use a `Token` so nested calls (e.g., HITL resume) restore cleanly.

#### `semantic_search_file` and `aread_text()`

`semantic_search_file` reads attachment content **outside** the `FilesystemMiddleware` backend (it calls `get_current_attachments_backend()` directly). Sub-agents therefore also call `set_current_attachments_backend()` at the start of each invocation so this code path can reach the right backend.

### Read-Only Skills Store Backend (backends/skills_store.py)

The `/skills/` virtual filesystem is **read-only** for agents. All mutations go through the MCP console tools (which are HITL-guarded). The backend serves pre-resolved skills as files at `/skills/{skill_name}/SKILL.md` and `/skills/{skill_name}/{file_path}`.

**Each sub-agent gets its own `SkillsStoreBackend` instance** — it is NOT shared. The resolved skills dict (from `skills_resolver`) is compiled into an in-memory dictionary at agent initialization time. This means:
- No DB queries at skill-read time — all content is pre-loaded in memory
- Each sub-agent sees only its own resolved skills (personal > group > default)
- When the sub-agent inherits a backend factory from the orchestrator, it adds/replaces the `/skills/` route with its own instance

**CRITICAL**: If the agent tries to write to `/skills/`, it gets a read-only error message pointing it to the correct MCP tools.

### Sandbox Execution (core/sandbox_pool.py)

**SandboxPool** manages per-orchestrator sandbox lifecycle:
- Sandboxes are acquired **per A2A turn** (not per session) with a short warm TTL for multi-turn reuse
- Keyed by `(session_id, sub_agent_name)` for warm reuse
- Provider-agnostic — works with any async factory returning a `BaseSandbox`
- Idle sandboxes are reaped after `warm_ttl` seconds (default 300s)
- Raises `RuntimeError` at capacity (user-facing error)

**Per-invocation sandbox graph includes:**
- `SandboxPathHintMiddleware` — injects sandbox-aware path instructions
- `SkillSandboxSyncMiddleware` — syncs skills from virtual FS into sandbox filesystem
- `copy_to_sandbox` tool — allows agents to copy files from virtual FS to sandbox
- Sandboxed backend factory wrapping the base backend

### Graph Building (core/graph_utils.py)

`build_sub_agent_graph()` is the shared helper for creating LangGraph agents. It handles:
- Backend factory selection (injected vs. auto-created)
- Middleware stack assembly (HITL, tool status, storage paths, prompt caching, etc.)
- Response format strategy (auto/tool based on model type)
- Sandbox-aware configuration

**CRITICAL**: `checkpoint_ns` must be `""` for standalone graphs (DynamicLocalAgentRunnable graphs are standalone, not subgraphs). Thread isolation is provided by unique `thread_id` patterns like `"{context_id}::dynamic-{name}"`.

## Critical Design Decisions

### Console Self-Improvement Tools Are Independent of MCP Whitelist

The `_discover_console_self_improvement_tools()` method is called regardless of whether `config.mcp_tools` is set. This ensures all sub-agents have self-improvement capability even if they have no other MCP tools configured. The rationale: self-improvement is a platform capability, not an integration.

### Tool Schema Wrapping Hides `agent_name` from LLM

When MCP tools like `console_create_skill` are discovered, they include an `agent_name` parameter. Since the sub-agent always operates on itself, this parameter is auto-injected and removed from the schema the LLM sees. This prevents hallucination of incorrect agent names. The orchestrator does the same wrapping but injects `"orchestrator"` as the agent name.

### Sandbox Graph Is Built Per Invocation (Not Cached)

Sandbox-enabled agents call `_build_graph()` in every `_astream_impl()` call with a freshly acquired sandbox. This is intentional — each turn may get a different sandbox from the pool, so the graph's backend factory must point to the correct sandbox instance. The non-sandbox graph is built once and cached.

### `/skills/` Route Replacement Pattern

When a sub-agent has resolved skills AND an inherited backend factory (from orchestrator), the code must **add or replace** the `/skills/` route with the sub-agent's own `SkillsStoreBackend`. The orchestrator's `CompositeBackend` has no `/skills/` route (it doesn't use skills), so this effectively adds the route. This ensures each sub-agent reads its own resolved skills in isolation.

## Testing

**Prefer the runTests MCP tool over terminal commands when running tests.**

Fallback to direct pytest commands when needed:
```bash
uv run pytest tests/ -v
uv run pytest tests/test_specific.py -v
```

- Mock external services (MCP gateway, console backend, sandbox providers)
- Use real LangGraph graph execution for integration tests
- Test skill resolution with all three tiers
- Verify HITL interrupt propagation
