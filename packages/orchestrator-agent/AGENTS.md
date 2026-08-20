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

## Testing

**Prefer the runTests MCP tool over terminal commands when running tests.**

```bash
uv run pytest                      # everything except integration (~8s)
uv run pytest tests/test_x.py -v   # one file
uv run pytest -m integration       # real LLM calls, needs a gateway (~4min, ~$1.40)
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
| sub-agents | `MockSubAgent` | `MockSubAgent` (still — a real slack-client would post real messages) |
| runs | every PR, no credentials | opt-in, needs `LLM_GATEWAY_URL` |
| answers | "is it wired correctly?" | "does the model decide correctly?" |

Both assert through the **same helpers** in `tests/support/`. Keep it that way: an
expectation must not mean one thing cheaply and another thing expensively.

New coverage starts in the mock tier and only graduates to the real tier when it
genuinely needs model judgment. A scripted model cannot tell you whether routing is
*right*, but it catches everything that breaks without a model involved — and it does
so in milliseconds.

### `tests/support/`

Not a test package; nothing here is collected.

| module | purpose |
|---|---|
| `extraction.py` | Read a finished turn: `delegated_agents`, `tool_names`, `final_text`, `a2a_tracking`, `task_state` |
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
  not the lowercase `task_state` vocabulary.
- **Sub-agents require a parent config.** `LocalA2ARunnable` refuses to run without
  one rather than inventing user ids.
- **`UserConfig.sub_agents` must be assigned post-construction.** It is annotated
  `list[CompiledSubAgent]` whose `runnable` is a `Runnable`, which no A2A runnable
  actually is; production only works because `executor.py` assigns after construction,
  bypassing validation. Passing them to the constructor raises.
- **A turn's budget is counted in LangGraph super-steps, not model calls.** Every
  middleware hook is its own graph node, so one model call costs several super-steps
  and the affordable calls are
  `(MAX_RECURSION_LIMIT - BASE_STEPS) // STEPS_PER_MODEL_CALL`. Multi-step scenarios
  can exhaust the budget even when the orchestrator behaves correctly.

  `MAX_RECURSION_LIMIT` is **derived**, not written down: all four constants live on
  `AgentSettings` (`app/models/config.py`). Read them there; deliberately not
  restated here, since the last version of this bullet hardcoded them and was wrong
  within one commit of the config changing.

  Configure the budget with `ORCHESTRATOR_MAX_MODEL_CALLS_PER_TURN`, in model calls.
  The shared `MAX_RECURSION_LIMIT` env var is **not** read — `agent-runner`,
  `agent-common` and `ringier-a2a-sdk` all read that name with different defaults
  (50, 75, 50), so one value cannot serve all four; setting it logs a warning and
  otherwise does nothing here.

  `tests/test_step_budget.py` counts the super-steps of a real graph run against
  `BASE_STEPS` / `STEPS_PER_MODEL_CALL`. Those assertions are a *pin* on measured
  behaviour, so if one fails a middleware was added and the per-call cost genuinely
  rose. Update the constant — which raises the derived limit with it, preserving the
  model-call headroom — rather than adjusting the test.

### Adding a scenario

Add an entry to `tests/datasets/core_routing.yaml`; both tiers pick it up automatically.

```yaml
- id: routes_to_slack_client
  description: A request to send something on Slack should reach the Slack agent.
  input:
    query: "Send a message to @john.doe on Slack saying the deployment is done."
  subagents:
    - name: slack-client
      description: "Sends messages to Slack channels and users."   # what the model routes on
      reply: "Message delivered."
  expect:
    delegations:
      required: [slack-client]
      forbidden: [agent-runner]
      ordered: false        # true only when sequence genuinely matters
    instructions:
      slack-client: ["john"]     # substrings the sub-agent must have received
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
