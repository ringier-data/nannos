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

### Every Delegation Is an A2A Task (ADR-0008)

A `task` call to a registry sub-agent opens one A2A task on it, named after the tool call (`delegation_task_id(conversation, tool_call_id)`, proposed via `SubAgentInput.proposed_task_id`). Local sub-agents run behind an **in-process A2A server** (`agent_common.a2a.local_server`: A2A SDK `DefaultRequestHandler` + `TaskStore` + `LocalSubAgentExecutor`); `LocalA2ARunnable.astream` is its client and yields the same typed `StreamEvent`s `A2AClientRunnable` yields from HTTP. The dispatch middleware never probes a sub-agent's checkpoint and never builds its `Command(resume)`:

- a local sub-agent's graph parking on an interrupt (tool approval, in-task auth, client action) comes back as the task in `input_required`/`auth_required` with the raw interrupts in `data.metadata["interrupts"]`; the dispatch calls the orchestrator's `interrupt()` with the first value (structured card, as before). On the replay LangGraph re-runs the same tool call; `tasks/get(delegation_task_id)` finds the parked task, `interrupt()` returns the answer, and it travels to the task as a message (`answer_to_human_message`). The sub-agent's executor fits it to the pending interrupt (`local_server.resume`) and resumes. Up to `MAX_SUBAGENT_RESUME_ROUNDS` pauses per delegation.
- a question asked **in words** (structured response `task_state: input_required`) carries no interrupts and reaches the model as a normal result; the reply continues the task (`a2a_tracking` keeps its `task_id` while not complete).
- results are typed (`TaskResponseData`): `a2a_metadata.state` is the protobuf enum **name** (`TASK_STATE_COMPLETED`); there is no JSON envelope in the message text any more.
- two `task` calls to the **same** agent in one assistant message both run: the executor serialises graph runs per conversation thread (`LocalA2AServer.thread_lock`), so the second is a follow-up on the agent's conversation. The only refusal left is server-side: a NEW task on a thread parked on a question is `rejected` (`PARKED_TASK_MESSAGE`). The `concurrent_task_refusal` tag, its skip sites and the one-task-per-agent prompt rule are gone.
- the embedded execute-only path (`stream_subagent`) drives `runnable.astream_graph` directly — that executor IS the A2A server for its sub-agent, so routing through `astream` would nest one server in another.
- `main.py` installs the orchestrator's task store into the local servers (`set_local_task_store`), so a parked local task survives a restart like the orchestrator's own.

Full flow, tables and sequence diagrams in `docs/subagent-flow.md`.

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

### Mid-Turn Notes: `notify_user`, Not a New Extension

A turn can plan, delegate and call tools for a minute while the user sees only mechanical lines ("Using search…") — nothing tells them the agent understood the request. `notify_user` (`agent_common/core/notify_user_tool.py`) is the deliberate channel: the model writes one or two sentences for the user, the tool emits them fire-and-forget on the LangGraph custom stream (`("user_note", {"message": …})`, exactly like `client_action`'s navigate/highlight), and the graph never pauses.

Three decisions worth keeping:

- **It rides the existing activity-log extension, with `kind="note"` in the message metadata** — not a new URN. Every client that renders the activity timeline (embed SDK `activity` part, Slack task cards, console-backend history) shows notes with zero client work, and a UI that reads `kind` can later style the agent's own words apart from a tool label. A new extension would have meant a registry entry plus a negotiation-header change in console-backend before a single note could appear.
- **It stays natively bound, out of `extra_static_ptc_tools`** — hence `get_static_tools(with_notify_user=True)` appending it outside `_static_tools_cache` (`core/graph_factory.py`). The whole value is that the model can emit the note *in the same step as its first real `task`/tool call*, so the work does not wait a round trip on it; reaching it through `eval` would put the note behind a PTC hop. The PTC guidance lists it with `task`/`write_todos`/the response tool as a control primitive for the same reason.
- **Risk-scored deterministically at 0** (`tool_risk_scorer.score_tool_risk`, next to the `client_action` short-circuit). A progress note touches no backend and returns nothing to the model, so an approval card in front of one would be absurd — and the LLM scorer must never be paid for it.
- The mechanical `Using notify_user…` line is suppressed via `_ACTIVITY_LOG_EXCLUDED_TOOLS` (`core/agent.py`), otherwise every note arrives twice: once as a tool label, once as itself.

Local sub-agents get the tool only when `client_action_enabled` (the embedded execute-only entrypoint, `dynamic_agent.py`), because there the sub-agent IS the top-level agent talking to the user; a delegated sub-agent is already narrated by the orchestrator's delegation lines. Notes are never the answer — that stays in `FinalResponseSchema` / `SubAgentResponseSchema`, and the prompt (`<keep_the_user_informed>`) says so explicitly, since a note carrying the answer shows the same text twice.

### Error Classification for Sub-Agent Failures

`ErrorClassificationMiddleware` classifies errors from sub-agent execution (auth failures, tool errors, etc.) to provide actionable feedback to the orchestrator's planning loop.

### Scheduled-Run Conversation Adoption: One Contract, One Continuity Mechanism

A conversation opened with a `scheduled_run` origin (conversation-origin extension) gets, besides the synthetic-history reconstruction, an `a2a_tracking` seed so the next delegation to the run's sub-agent **continues the run's own conversation** (`_validate_scheduled_run_origin` + `_build_adoption_seed` in `app/core/agent.py`). The mechanism alone is not enough — the model must be TOLD about it, or its "sub-agents are stateless" prior wins and it role-plays the sub-agent instead of delegating (observed live: asked to continue a run's number-guessing game, it invented its own secret via `eval`). So the synthetic history (`_build_scheduled_run_history`) states, when adoption validated, that delegating resumes the run's memory; and when the provenance's `task_state` is `input_required` (the run ended asking the user a question — agent-runner reports the terminal state in its result metadata, clients persist and forward it), the framing flips from "output was delivered" to "forward the user's reply to the sub-agent".

The seed is the same for every adoptable kind: `{"context_id": <run ctx>, "is_complete": True, "sub_agent_id": ...}`.

- **Remote agents** — agent-runner dispatched the run with the run task's contextId on the wire (`_run_remote_agent`), so the executing server checkpoints under exactly that id and the `A2AClientRunnable` waterfall resumes it.
- **Local/automated agents** — agent-runner executes the run on the thread `local_sub_agent_thread_id(run ctx, name)` = `{ctx}::dynamic-{name}` (`agent_common/a2a/threads.py`), the very thread `DynamicLocalAgentRunnable.get_thread_id` derives from a seeded context id, so the next delegation lands on the run's conversation with **no checkpoint copying**. The fork-on-adopt that used to stand in for this (and its shared-Postgres-schema requirement) is gone; a thread whose last turn died mid-tool is sealed in the next turn's *input* (`seal_dangling_tool_calls` in `_astream_impl`), never by editing the checkpoint. Runs executed before the convention change live on bare `{ctx}` threads and start blank when adopted — accepted, one-time.
- **Foundry** — not adoptable; continuity is a `foundry_session_rid` the provenance doesn't carry.

Automated (scheduler-only) sub-agents carry `interactive=False` (`registry.py`) and are registered into a conversation **only** when it adopted one of their runs (`adopted_sub_agent_ids` through `build_runtime_context`) — for the rest of that conversation they behave like any local agent; everywhere else they stay invisible. The ids are validated server-side on the blank first turn, then **re-derived on every subsequent turn — HITL resumes included — from the `sub_agent_id` stamped into the persisted a2a_tracking adoption record** (`_adopted_sub_agent_ids_from_tracking`); deriving them from the origin DataPart alone would deregister the agent after turn one and drop the approval of its own first delegation's interrupt.

**Open alternative (unmeasured) — serving local/automated sub-agents from agent-runner over HTTP.** The in-process server's executor (`LocalSubAgentExecutor`) is an ordinary A2A `AgentExecutor`, so agent-runner could mount it behind HTTP and the orchestrator could reach every sub-agent through `A2AClientRunnable`; adoption and HITL would then be identical for every kind, in every process. The argument against it is per-turn latency: a cold harness build on agent-runner's side (config fetch, token exchange, MCP discovery, graph compile) on top of the orchestrator's, versus in-process delegation reusing the warm per-user discovery cache, exchanged tokens, pooled checkpointer, gateway clients, and session-keyed sandboxes. The dominant term is not a guess: an earlier latency measurement found MCP discovery to be the largest contributor to time-to-first-token, and the per-user discovery cache keyed on an entitlement version (#206) exists to keep it off the turn. Serving interactive sub-agents from agent-runner would either replicate that cache there or pay the cold path on every delegated turn. What has NOT been measured is the HTTP path itself; a fair benchmark compares a warm orchestrator turn with a warm agent-runner turn behind an equivalent cache, plus the cold first-delegation case. Until then, in-process stands.

Two cross-cutting invariants: the seed key is **`runnable.tracking_key`** (`agent_common/a2a/base.py`) — the single home of the name-with-spaces-stripped convention shared by the registry key (`discovery.py`), the `a2a_tracking` writers, and `_extract_tracking_ids`' reader; never re-derive it from a name. And the origin DataPart is untrusted: the job/run are re-resolved via console-backend under the authenticated user's token (single-run endpoint `GET /api/v1/scheduler/jobs/{id}/runs/{run_id}` — the run *listing* is capped to the newest 50), the job's server-side sub-agent binding must match, and the **server-stored** `conversation_id` is what gets seeded — the DataPart's `context_id` plays no part.

## Testing

**Prefer the runTests MCP tool over terminal commands when running tests.**

```bash
uv run pytest                      # everything except integration (~8s)
uv run pytest tests/test_x.py -v   # one file
uv run pytest -m integration       # real LLM calls, needs a gateway (~4min, ~$1.40)
uv run pytest -m integration -n 8  # the same tests, concurrently (~3x faster)
```

Integration tests are **collected on every run but deselected** by `-m "not integration"`
in `addopts`. They used to be hidden with `--ignore`, which let an a2a-sdk migration
break three imports in `tests/integration/` unnoticed for months. Never go back to
`--ignore`: breakage must be visible even when the tests don't run.

That default is a convenience, **not** the spend guard. `-m` is last-wins, so any
user-supplied expression replaces it, and every integration module also carries
`slow` — so `-m slow` would select the integration directory and nothing else.
`tests/integration/conftest.py` therefore also requires the tier to be *requested*:
`-m integration`, or `RUN_INTEGRATION_TESTS=1` when selecting by path or keyword.
`-n <workers>` works on any selection, not just this tier — the unit suite runs
clean under it (1023 passed, 12.6s to 7.7s). The gain there is small because
those tests are import- and CPU-bound, so worker startup eats most of it;
nothing runs in parallel unless you ask for `-n`.

Where it pays is the integration tier. Model-parametrized tests are almost
entirely network wait on independent aliases, so a serial sweep costs the sum of
the fleet where it could cost its slowest member. `-n <workers>` (pytest-xdist) makes them concurrent — measured
141s to 45s on `test_tool_risk_scoring.py`. Spend is unchanged: the same calls
are made, just not one at a time.

The integration conftest tags every item carrying a `model_type` param with an
`xdist_group` named after the alias, so all tests for one model stay on one
worker. That keeps per-model memoization intact (otherwise a score shared
between two tests is billed twice — measured 9.7k tokens against 5.0k for the
same three aliases) and holds each alias to one in-flight request, since fanning
several at a single Bedrock model earns throttling that surfaces as a 500.

Two details there are easy to get wrong, and both fail *silently* — grouping
simply stops happening, with no error:

- The marker is added from `pytest_itemcollected`, not from
  `pytest_collection_modifyitems`. xdist turns `xdist_group` into a nodeid
  suffix from its own `pytest_collection_modifyitems`; its worker plugin
  registers after conftests load, and pluggy calls implementations
  last-registered-first, so xdist's runs before ours and finds no markers.
  `pytest_itemcollected` fires from `Session.genitems`, which finishes before
  any `pytest_collection_modifyitems` — a phase guarantee, rather than the
  relative hint `tryfirst` gives (which only orders against implementations that
  do not also claim it, and `cacheprovider`, `stepwise` and two xdist plugins are
  all in that hook). It is also called only for items in this directory, so the
  tier scoping is structural rather than a path check.
- `--dist=loadgroup` lives in `addopts`, not in a conftest. xdist hands workers
  the original command line, so a `config.option.dist` mutated at configure time
  never reaches the worker that actually reads it. It is inert without `-n`.

Confirm grouping is live by looking for `@<alias>` suffixes on the nodeids in the
report; without them, each alias is being scored once per test.

Token accounting reaches the controller on the report's `user_properties`;
writing straight to the `EvalSession` from a fixture would be invisible under
`-n` and every row would read `-`.

The predicate lives in `tests/support/marker_gate.py` and is pinned by
`tests/test_marker_gate.py`; it fails closed, since a skipped test is cheaper than
a surprise bill.

The same predicate keeps the unit loop network-free. Because the directory is
collected, `tests/integration/conftest.py` is *imported* on every run — and it
probes the gateway at import, since parametrize needs the model list while
collecting. That probe is now skipped unless the tier was requested (+4.8s
otherwise, when the gateway hostname does not resolve — DNS is not bounded by the
2s socket timeout). It has to be decided before collection, so the root
`tests/conftest.py` stashes the `-m` expression in `pytest_configure`: that is the
only hook that runs before a subdirectory conftest is imported. Keep it there.

- Mock A2A transport for sub-agent communication tests
- Use real graph execution for middleware integration tests
- Test HITL interrupt flow end-to-end
- Verify `GraphRuntimeContext` construction for different user configs

### Two tiers, one assertion vocabulary

| | mock tier | real tier |
|---|---|---|
| lives in | `tests/` | `tests/integration/` |
| model | `ScriptedChatModel` | live, via the gateway |
| sub-agents | `MockSubAgent` | `MockSubAgent` (still — a real Slack-posting sub-agent would post real messages) |
| runs | every PR, no credentials | opt-in, needs `LLM_GATEWAY_URL` |
| answers | "is it wired correctly?" | "does the model decide correctly?" |

Both assert through the **same helpers** in `tests/support/`. Keep it that way: an
expectation must not mean one thing cheaply and another thing expensively.

New coverage starts in the mock tier and only graduates to the real tier when it
genuinely needs model judgment. A scripted model cannot tell you whether routing is
*right*, but it catches everything that breaks without a model involved — and it does
so in milliseconds.

### Test rigor

Four rules, in descending order of how much damage breaking them does. The first
one is the whole section; the rest are its common shapes.

**1. A test must not replace the system whose behaviour it asserts.**

Ask it directly: *does the setup stub out the thing the assertion is about?* If
yes, the test proves nothing however green it is, and — worse than useless — it
reports confidence it does not have.

`tests/test_hitl_reject_turn.py` is the worked example. It used to build its own
`StateGraph` including its own router, then assert that rejected tool calls do not
execute. But a rejected call *stays* in the AIMessage (langchain's
`_process_decision` returns the tool call, not `None`) and is answered with a
synthetic error `ToolMessage`, so non-execution depends entirely on a router
skipping already-answered calls. Supplying that router is testing your own copy.

Measured: disabling the orchestrator's HITL guard entirely — every guarded tool
executing with no approval — left the whole 796-test suite green. Only the
rewrite against the real graph caught it, 7 tests failing.

Corollary: **never rebuild production topology in a test.** `scripted_graph()`
compiles the real `GraphFactory` graph in ~21ms. A hand-copied graph, router or
middleware stack is a copy that drifts, and it drifts silently.

**2. Test a provider-dependent claim with a pin plus recorded evidence.**

Some claims are only true of a real provider — that a structured-output method
works, that a schema is accepted. A stub cannot answer those, and hitting five
providers on every commit is not affordable. So split it:

- the **pin** is cheap and permanent: assert the request was *shaped* correctly
  (e.g. `method="function_calling"` was passed). Mutation-check it, then it runs
  free forever and catches the regression.
- the **proof** is expensive and recorded once: the live results, per model and
  provider, in the PR body.

PR #202 is the reference: a five-model table showing a 400 from Azure and
silently-empty output from Bedrock, next to a one-line pin in CI.

**3. Label a pin as a pin.**

A test that asserts "we pass X to third-party Y" is legitimate and worth having —
it is *not* evidence that Y then behaves. Say which one it is in the docstring.
`TestLangchainHITLContract` in `test_hitl_reject_turn.py` is labelled this way,
and kept precisely because the real-graph tests cannot tell you whether a
regression is upstream or ours.

**4. Know the mock tier's blind spot.**

Its final response is scripted *from* the expectations, so anything derived from
`expect` is true by construction there. `task_state: failed` in a dataset
scenario cannot fail in the mock tier — which is why
`_assert_outcomes_propagated` asserts on the sub-agent's returned message
instead, since that comes from the real dispatch path in both tiers.

If you cannot state what a test would catch that nothing else would, it is not
ready. Mutating the code it covers is the cheapest way to find out.

### `tests/support/`

Not a test package; nothing here is collected.

| module | purpose |
|---|---|
| `extraction.py` | Read a finished turn: `delegated_agents`, `tool_names`, `final_text`, `a2a_tracking`, `task_state`, `interrupted_tools` |
| `mock_subagents.py` | `MockSubAgent` — subclasses `LocalA2ARunnable`, so it travels the real dispatch path |
| `scripted_model.py` | `ScriptedChatModel` — replays canned responses, records what tools were bound |
| `graph_harness.py` | `scripted_graph()` builds a **real** `GraphFactory` graph with a scripted model |
| `scenarios.py` | Loads `tests/datasets/*.yaml`; `assert_scenario()` is the shared vocabulary |
| `eval_report.py` | Pass-ratio gate and cost reporting |
| `usage.py` | `UsageRecorder` — token accounting via `callbacks` in the graph config |

### Facts that are easy to get wrong

Learned the hard way; each cost real debugging time.

- **Delegation is one tool.** There is no `delegate_to_x`. It is `task` with
  `args["subagent_type"]`, and the instruction in `args["description"]`.
- **Sub-agent tools are invisible.** The orchestrator only ever sees `task`,
  `write_todos`, `FinalResponseSchema`, `get_current_time`, docstore and MCP tools.
  Expecting `send_slack_message` in orchestrator state can only ever fail.
- **Assertions must be turn-scoped.** The checkpointer accumulates history, so an
  unscoped read happily passes on a delegation from two turns ago. The helpers default
  to the current turn.
- **`structured_response` is a pydantic instance, not a dict** (the graph sets
  `response_format`). Read it with the pydantic API.
- **`include_subagent_output=true` means `message` is EMPTY** by design — the
  sub-agent's output is appended downstream. `final_text()` reproduces that; reading
  `message` alone reports an empty answer for a turn that answered at length.
- **`a2a_tracking[...]["state"]` is the protobuf enum name** (`TASK_STATE_COMPLETED`),
  not the lowercase `task_state` vocabulary — for local and remote sub-agents alike, since
  both results are typed `TaskResponseData` read the same way.
- **Sub-agents require a parent config.** `LocalA2ARunnable` refuses to run without
  one rather than inventing user ids.
- **`MockSubAgent` travels the real in-process A2A server.** Its `astream` opens a task,
  goes through the SDK request handler and task store, and comes back typed — so a
  routing test also exercises the production lifecycle. `MockSubAgent(approval="tool")`
  parks on a tool-approval interrupt the way a risk-gated tool does; see
  `tests/test_delegation_task_lifecycle.py` for the park → replay → resume path through
  the real graph.
- **`UserConfig.sub_agents` must be assigned post-construction.** It is annotated
  `list[CompiledSubAgent]` whose `runnable` is a `Runnable`, which no A2A runnable
  actually is; production only works because `executor.py` assigns after construction,
  bypassing validation. Passing them to the constructor raises.
- **A turn's budget is counted in LangGraph super-steps, not model calls.** Every
  middleware hook is its own graph node, so one model call costs a whole lap of the
  graph. Multi-step scenarios can exhaust the budget even when the orchestrator
  behaves correctly.

  The limit is **derived from the compiled graph** by
  `agent_common/core/step_budget.py`, which classifies nodes by hook suffix
  (`.before_model` / `.after_model` per model call, `.before_agent` / `.after_agent`
  per turn, plus `model` and `tools`). There is no constant to maintain: adding a
  middleware raises the per-call cost and the limit follows it. Read that module
  rather than trusting a number restated here — the previous version of this bullet
  hardcoded three and was wrong within one commit of the config changing.

  It lives in `agent-common` because the sub-agent paths had the same bug: the
  *same* `build_sub_agent_graph` output carried a hand-written 75 in `dynamic_agent`
  and a hand-written 50 in `agent-runner`, so a scheduled run of a sub-agent died on
  `GraphRecursionError` where a delegated one succeeded.

  Configure the budget with `ORCHESTRATOR_MAX_MODEL_CALLS_PER_TURN`, in model calls.
  Every consumer now has its own name — `SUB_AGENT_MAX_MODEL_CALLS_PER_TURN` for
  in-process sub-agents, `AGENT_RUNNER_MAX_MODEL_CALLS_PER_TURN` for scheduled ones
  — so raising one cannot silently move another. The defaults differ by how much
  work the turn does (planning 25 < in-process sub-agent 40 < scheduled run 125) and
  are defined together in `agent_common/core/step_budget.py` so they can be compared. The old shared `MAX_RECURSION_LIMIT`
  (and `SUB_AGENT_RECURSION_LIMIT`) are read by nothing in this repo; setting either
  logs a warning and otherwise does nothing. `MAX_RECURSION_LIMIT` is still read by
  the externally published `ringier-a2a-sdk`, so it must not be unset on the strength
  of that warning alone.

  `tests/test_step_budget.py` measures the super-steps of a real graph run and
  asserts the derived budget covers them with under one model call of slack (the
  sub-agent graph gets the same treatment in `agent-common`). A failure there means
  the *derivation* is wrong — most likely a new node the classifier does not
  recognise — not a number to bump.

### Adding a scenario

Add an entry to `tests/datasets/core_routing.yaml`; both tiers pick it up automatically.

```yaml
- id: routes_to_slack_notifier
  description: A request to send something on Slack should reach the Slack agent.
  input:
    query: "Send a message to @john.doe on Slack saying the deployment is done."
  subagents:
    - name: slack-notifier
      description: "Sends messages to Slack channels and users."   # what the model routes on
      reply: "Message delivered."
  expect:
    delegations:
      required: [slack-notifier]
      forbidden: [revenue-analyst]
      ordered: false        # true only when sequence genuinely matters
    instructions:
      slack-notifier: ["john"]     # substrings the sub-agent must have received
    tools:
      required: [get_current_time]
    task_state: completed
    response_contains: ["8.2"]
```

Rules that keep scenarios from becoming flaky:

- **Never assert on wording the model chooses.** An early scenario required `"4"` and
  gemini answered *"Two plus two is four."* — a correct answer failing on
  representation. Assert stable specifics (a name, an identifier, a number that must
  appear), or assert nothing about the prose.
- **`instructions` are substrings, not equality.** The model phrases hand-offs freely.
- **`subagents[].description` is the routing signal** in the real tier. Keep it close
  to the real agent card, or you are testing a fiction.
- **Name sub-agents after things that are actually delegable** — registry sub-agents,
  system-seeded or user-created. Clients (`client-slack`, `client-email`) call the
  orchestrator and render its reply; they serve no agent card. `agent-runner` is called
  by console-backend's scheduler. Nothing delegates to either, so naming one documents
  a path that does not exist. Nothing in the harness validates this, which is exactly
  why it needs saying: the tests pass either way.
- **Include negative expectations.** `forbidden` is what makes a routing assertion
  falsifiable — but only for agents that are actually registered, or it proves nothing.

The mock tier also lints the dataset: it rejects unobservable tools, instructions keyed
to unregistered sub-agents, and contradictory required/forbidden pairs. A scenario that
fails there is malformed, not a real finding.

### The pass-ratio gate

Real-LLM tests fail occasionally for reasons that are not defects. Rather than a rerun
plugin — which retries until green and so *hides* flakiness — every test runs once and
the session is judged on the aggregate ratio.

```bash
EVAL_MIN_PASS_RATIO=0.75                                   # default
EVAL_REPORT_PATH=../../logs/eval-report.json               # optional JSON artifact
```

- Skips are excluded: a skip is not evidence either way.
- `@pytest.mark.strict` exempts a test from the ratio — it must pass. Use it for
  behaviour that is not supposed to be probabilistic, and to stop a permanently-broken
  test from hiding behind healthy siblings.
- A run that passes the gate with failures present says so explicitly. **Read those
  failures** — the gate tolerates sampling noise, it does not certify correctness.

Cost is reported per test. Note that ~97% of spend is *input* tokens (system prompt and
tool schemas re-sent on every model call), so scenario cost is roughly fixed regardless
of complexity — budget ~20k tokens per scenario per model.

### Setting up the real tier

```bash
./scripts/start-local.sh          # local LiteLLM gateway on :4000
```

Then two lines in `packages/orchestrator-agent/.env` (gitignored — verify with
`git check-ignore` before adding anything, this is a public repo):

```
LLM_GATEWAY_URL=http://localhost:4000
LLM_GATEWAY_API_KEY=sk-nannos-local
```

Provider credentials (Bedrock/Azure/Vertex) belong to the **gateway process**, not the
test process. Never put them in a test env file. Which models exist comes from the
gateway registry, so pin `litellm-local-models.yaml` to what the deployed gateway
serves — otherwise a scenario tuned locally proves less than it appears to.

With no gateway, the whole directory skips with a reason saying so.

### Framework choice

Plain **pytest** plus the dataset and shared assertions above. LangSmith stays for
tracing and experiment history (`@pytest.mark.langsmith`), not as the test framework.

Rationale, from actually using it: LangSmith's value here is the trace UI, and its
pytest plugin reaches out during *setup* to create a test suite, so without a valid
`LANGSMITH_API_KEY` every marked test fails for a reason unrelated to the code (hence
`LANGSMITH_TEST_TRACKING=false` when no real key is present). Gating, cost reporting and
dataset assertions are all things we need to own regardless, and they work whether or
not tracing is on. A dedicated eval framework would add a second vocabulary next to
pytest for no coverage we cannot already express.
