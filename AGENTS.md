# Alloy Infrastructure Agents

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Common Conventions

- **Python Commands**: Always use `uv` for all Python operations (`uv sync`, `uv run pytest`, `uv run python`)
- **File Writing**: NEVER use heredoc (`cat << EOF`) to write files — causes fatal errors. Use incremental edits instead.
- **Node Commands**: `npm ci` to install, `npm run build`, `npm test` (Jest), `npm run lint` (ESLint)
- **DB Migrations**: All services use **Rambler** for SQL migrations (`sqlmigrations/` dirs). Migrations run automatically on container startup via `entrypoint.sh`.
- **API Clients**: Frontend packages auto-generate TypeScript API clients from OpenAPI specs (`npm run gen-sdk`, config in `openapi-ts.config.ts`)
- **Docker Registry**: `ghcr.io/ringier-data/nannos-<package-name>`
- **Git Tags**: `<package-name>/v<semver>` (e.g., `orchestrator-agent/v0.10.0`)
- **Versioning**: Each package is versioned independently. Version lives in `pyproject.toml` (Python) or `package.json` (Node). Use `just changed` to see what needs release, `just release` to bump+tag+build all changed packages.
- **npm**: `embed-sdk` is released to the public npm registry as `@nannos/embed-sdk` — `just release` publishes it after the image pushes (needs npm credentials; `just publish-npm embed-sdk` retries a publish alone).
- **SDK hosts**: apps in other repos that install `@nannos/embed-sdk` (cockpit) are registered as gitignored `hosts/<name>` symlinks. `just hosts` shows their state, `just host-link <name>` develops them against this checkout (node_modules only — never package.json/lockfile), and `just release` / `just host-bump <name>` moves them onto the published version.

## Repository Overview

This is a monorepo for **Nannos** — a multi-agent AI orchestration platform built on the **A2A (Agent-to-Agent) protocol**. Users interact through clients (web console, Slack, email); a central orchestrator plans tasks and delegates to specialized sub-agents.

### Architecture

```
Clients (console-frontend, client-slack, client-email, client-google-chat)
    │ REST/WS/A2A
    ▼
Console Backend (admin hub, API, scheduler)
    │ A2A
    ▼
Orchestrator Agent (LangGraph — plans & delegates)
    │ A2A
    ▼
Sub-Agents (voice-agent, agent-creator, task-scheduler, user-created agents)
```

`agent-runner` is deliberately absent from that last row: the scheduler calls it,
and it runs sub-agent jobs itself, so nothing delegates to it from above. Clients
are likewise one-directional — they call the orchestrator and render its reply;
none of them serves an agent card, so none is a delegation target.

### Packages

| Package | Lang | Type | Port | Purpose |
|---------|------|------|------|---------|
| `ringier-a2a-sdk` | Python | Lib | — | A2A protocol + OAuth2/JWT auth. Consumed by all Python services |
| `agent-common` | Python | Lib | — | LLM model factory (OpenAI/Bedrock/Azure/Google), LangGraph checkpoints, MCP adapters, sandbox pool, skills resolution, self-improvement protocol. Consumed by all Python agents |
| `orchestrator-agent` | Python | A2A Svc | 10001 | **Central coordinator**. LangGraph state machine, discovers sub-agents, plans & delegates tasks, multi-turn conversations |
| `agent-creator` | Python | A2A Svc | 8080 | Guides users through designing new AI agents, uses MCP tools to create them |
| `agent-runner` | Python | A2A Svc | 5005 | Executes scheduled background jobs against sub-agents. Called by console-backend scheduler |
| `console-backend` | Python | REST/WS | 8080 | Admin hub: agent CRUD, conversations, file uploads, scheduler, user/group mgmt, Keycloak integration, usage tracking |
| `console-frontend` | React/TS | SPA | 8081 | Admin web UI. Vite + Tailwind + Radix UI + React Router 7 |
| `client-slack` | Node/TS | A2A Svc | 3000 | Slack bot (Bolt framework + Koa REST). Per-user OIDC auth, thread context forwarding |
| `client-slack-frontend` | React/TS | SPA | 8080 | Slack admin config UI |
| `client-email` | Node/TS | A2A Svc | 3001 | Email client via AWS SES/SNS. Express 5 |
| `client-google-chat` | Node/TS | A2A Svc | 3000 | Google Chat bot. Express 5, per-user OIDC auth |

### Dependency Chain

```
ringier-a2a-sdk → agent-common → { orchestrator-agent, agent-creator, agent-runner }
                                              ↕ A2A
                                      console-backend
                                     ↕ REST        ↕ A2A
                              console-frontend    client-slack, client-email, client-google-chat
```

### Key Design Decisions

- **Zero-trust auth**: Every service validates JWT tokens independently via JWKS. No implicit trust between services.
- **A2A protocol**: All inter-agent communication uses authenticated A2A messages, not direct function calls.
- **Model Gateway**: All LLM traffic routes through a single **LiteLLM Proxy pod** (the "Model Gateway"), which holds provider routing/credentials and exposes an OpenAI-compatible inference API plus a management API for runtime model CRUD. App-side, every client is therefore a `ChatOpenAI` pointed at the proxy — the app never names a provider model id or branches on provider. `agent-common`'s model factory builds these gateway-backed clients. Provider-specific client checks and in-process caching middleware are effectively no-ops (everything is `ChatOpenAI`). Vocabulary:
  - **Model Alias** — the stable, provider-agnostic name an app requests (e.g. `claude-sonnet-4.6`); the Gateway resolves it to a concrete provider + provider model id.
  - **Capability** — what a model can do (input modalities, extended-thinking support/levels, context window); drives the model-picker UI. Stored in the Gateway's `model_info`.
  - **model_info** — per-model metadata on the proxy (Capability + base cost); source of truth for routing + capability. Written/edited *exclusively* through console-backend, never hand-edited on the proxy.
  - **Rate Card** — console-backend's billing record keyed on `(provider, model_name, billing_unit)`. Richer than the Gateway's cost map: supports per-sub-agent pricing and time-versioned rates. A Rate Card must exist before a model goes `active`. (`model_name` stores the Model Alias.)
  - **Usage Event** — one LLM call's measured consumption (token breakdown + model + Cost Attribution), captured proxy-side via a LiteLLM `CustomLogger`; costed against the Rate Card.
  - **Tier group** — a chat tier's *ordered* models: the tier's default (`model_defaults`)
followed by its failover chain (`model_tier_fallbacks`). Declared by console-backend onto the proxy
as one whole-list `POST /config/update` of every chain (DB-backed, so it cannot drift against the
DB-backed model registry; never the per-entry `/fallback` endpoints, which read-modify-write a
60 s-cached copy of all chains and lose edits made in quick succession) and
executed entirely by LiteLLM — retries, cooldown, then the next alias. Within-tier only: a tier
never chains into another tier, because availability and cost/quality are separate axes. Chat tiers
only — an embedding call must never fail over, since a second model's vectors insert cleanly into
the same pgvector index and silently degrade search. See ADR-0014.
  - **Capability record** — what the registration probe *saw* a deployment accept
(`model_info.nannos_capabilities`: forced tool choice, `response_format`, the thinking-off switch —
`always_on` when nothing turns thinking off, which the console shows as a locked Extended
Thinking toggle and the gateway sends at the measured `thinking_floor` effort, or `unsupported` when
every off request is refused — and thinking replay), as opposed to a Capability the admin declares or a provider map claims. The shapes
probed are the harness's own, defined once in `ringier_a2a_sdk.model_capabilities` and read by
console-backend (writes the record), the gateway hook (rewrites a request for the deployment that
serves it — from the record alone, never from the model's name; an unprobed deployment is left
as sent) and agent-common (picks the shape the alias accepts). An unavoidable shape failing refuses
registration; a routable one failing is recorded. A model recorded as rejecting `response_format`
cannot default `chat`/`chat:low` or join their chains. See ADR-0015.
  - **Failover** vs **alias degradation** — *failover* is runtime (a live alias's provider is
unavailable, the gateway tries the next in the tier group); *alias degradation* is registry-time (a
**retired** alias resolves to its tier's successor, `resolve_chat_model`). Both were once called
"graceful degradation"; they share no mechanism.
  - **Cost Attribution** — who to bill (user / sub-agent / sub-agent config version / conversation / scheduled job); travels to the proxy per-request as `spend_logs_metadata`. Set at request boundaries and refined per model call by `GatewayAttributionMiddleware` (derives it from the call's own LangGraph tags), so in-process sub-agent calls bill to the sub-agent, not the orchestrator.
- **Reasoning effort**: extended thinking uses LiteLLM's unified `reasoning_effort`; the app keeps only a small `thinking_level → reasoning_effort` map (budgets are provider-determined).
- **Streaming watchdog**: a mandatory client-side inter-chunk watchdog bounds streaming, because the proxy silently ignores `stream_timeout` on Bedrock streaming (LiteLLM #23375); proxy timeouts are a best-effort outer bound only.
- **Stateless services**: All conversation/checkpoint state persists in DynamoDB + PostgreSQL, enabling horizontal scaling.
- **MCP for tools**: Model Context Protocol pattern allows dynamic tool discovery and composition at runtime.
- **Multi-stage Docker builds**: Python services use `uv` for fast cached installs; shared libs (`ringier-a2a-sdk`, `agent-common`) are passed as Docker build contexts.
- **Skills registry as source of truth**: All skill content lives in `skill_registry` table. Docstore is a runtime cache. Activations are content-hash pinned for version control.
- **HITL-guarded self-improvement**: All skill/playbook mutations require user approval via `HumanInTheLoopMiddleware` interrupt. Agents propose, users approve/edit/reject.
- **Sandbox per A2A turn**: Sandbox-enabled agents acquire a fresh sandbox per invocation (not per session) via `SandboxPool`. Sandboxes are warm-cached by `(session_id, sub_agent_name)`.
- **Scheduled-run conversation adoption**: when a reply under a delivered notification opens an orchestrator conversation (conversation-origin extension), the orchestrator re-validates the job/run server-side under the user's token and seeds `a2a_tracking` so a follow-up delegation **continues the run's own conversation**. One contract, two mechanisms: **remote** runs resume by contextId on the executing server (agent-runner sends the run task's contextId on remote dispatches, so `scheduled_job_runs.conversation_id` names a real conversation there); **local/automated** runs are **forked** — their checkpoint (bare `thread_id = run ctx` in the checkpoint tables both services share) is copied once onto the conversation's own thread by the dispatch middleware, never re-pointed and never seeded as a raw `context_id` (that desynchronizes the HITL checkpoint probe). Automated sub-agents become delegable only inside the adopting conversation. Foundry lacks a persisted session rid and is not adopted. Details in `packages/orchestrator-agent/AGENTS.md`.
- **Single graph per model type**: The orchestrator uses ONE compiled graph per model, shared across all users. Tools are injected at runtime via `GraphRuntimeContext`, not baked in.
- **Per-user discovery/registry cache keyed by an entitlement version**: The orchestrator memoizes capability discovery (MCP tools + sub-agents) and the registry user lookup per user (`app/core/discovery_cache.py`), keyed by `cache_key(user_sub, groups, sub_agent_config_hash, policy_version, entitlement_version)` — the user lookup and the embedded runnable additionally by `settings_version`, a digest of the user's `user_settings` row served by the same endpoint, so a preference change (model, thinking, language, custom prompt) applies on the next turn without evicting discovery — TTL-bounded and additionally bounded by the user token's `exp`. `entitlement_version` is an opaque stamp console-backend derives from the rows that decide a user's entitlements (`GET /api/v1/auth/me/entitlement-version`, `services/entitlement_version.py`: role, whitelist/bypass rules by value, group memberships, group default agents, agents and catalogs shared with the user's groups, owned catalogs, sub-agent activations + the served agents' approved version and content hash — and only those, so a login re-upsert or a timezone change does not move it or evict discovery). The orchestrator fetches it once per turn *before* the cache lookups, so any entitlement change is picked up on the user's next turn on every replica — no push-based invalidation, nothing for a mutation site to remember. The one entitlement not held in the console DB (group → MCP-gateway server access) is covered by bumping `users.entitlements_touched_at` for the group's members when the console grants/revokes it. Gateway-side catalogue changes made outside the console remain TTL-bounded (the TTL's only remaining job). `sub_agent_config_hash` is playground-only (the console's "test this config version" mode), not a digest of the user's sub-agent set.

### Infrastructure Requirements

- **PostgreSQL**: `console` schema (pgcrypto), `docstore` schema (pgvector) shared by agents, `slack_client`, `email_client`, and `google_chat_client` schemas
- **Auth**: Keycloak (or any OIDC provider). Per-service OIDC clients: `orchestrator`, `agent-console`, `slack-client`, `email-client`, `google-chat-client`
- **AWS** (production): Postgresql (checkpoints), S3 (files), SES (email), Secrets Manager, SSM
- **LLM providers**: Local (OpenAI-compatible), AWS Bedrock, Azure OpenAI, Google Vertex AI — configurable via env vars
- **Optional**: MCP Gateway (tool extensions), LangSmith (tracing)

### Directory Layout Patterns

**Python services** follow: `main.py` (entry) → `app/` or `agent/` → `core/` (agent, executor, graph) + `models/` + `middleware/` + `handlers/`

**Node services** follow: `src/app.ts` (entry) → `config/` + `services/` + `storage/` + `controllers/` + `middleware/`

**Frontend SPAs** follow: `src/main.tsx` → `pages/` + `components/` + `api/generated/` + `hooks/` + `contexts/`

### Local Development

**Your stack is a slot (ADR-0016).** From your worktree, `just up` claims a slot (1–8) and starts a
full stack of *your branch's* code on its own ports, databases, Model Gateway and cookies, beside
every other stack. It needs no TTY and asks nothing: it returns once every service is ready (or
fails at once, naming the step or service that failed) and prints the slot's URLs as JSON. Running
it again from the same worktree just prints the running slot, so call it whenever you need the
stack — do not probe ports and reuse whatever answers (slot 0 on `:5173`/`:5001` may be running
another branch).

```bash
aws sso login --profile "$AWS_PROFILE"   # if the SSO session expired (SSM secrets, Bedrock)
just up                  # remote IdP from .env (needs the slot URIs registered there)
just up --local-idp      # local Keycloak instead: test@local.dev / password
```

Slot N: console `http://localhost:4N173`, backend `:4N001`, orchestrator `:4N010`, runner `:4N005`,
gateway `:4N400`; databases `console_sN` / `docstore_sN` on the shared PostgreSQL (`:5401` /
`:5402`); logs in `~/.nannos/slots/N/logs/<service>.log` (setup steps too: `migrate-console.log`,
`keycloak-setup.log`, …). Services hot-reload on edit. What the stack talks to comes from `.env`:
`AWS_PROFILE` → cloud models (Bedrock/Azure/Vertex) and SSM secrets, `OPENAI_COMPATIBLE_BASE_URL` →
a local LLM, `OIDC_ISSUER` → the remote IdP (else the local Keycloak).

The stack is one process-compose definition, `scripts/local-dev/process-compose.yaml` (setup steps
as one-shot processes, then the services); `scripts/start-local.sh` assembles its environment and
secrets. Each stack has a control socket, `~/.nannos/slots/N/pc.sock`:
`process-compose process list -u ~/.nannos/slots/N/pc.sock` shows every process's state,
`process-compose process restart <name> -u …` restarts one, and
`process-compose attach -u …` opens the TUI on it — what the user runs to look into your stack.
`just graph` draws the stack's dependency tree with each process's state, and `just startup` shows
what held its start up (both: your slot, else slot 0). Processes are grouped by role, numbered in
startup order (namespaces `1-infra`, `2-deps`, `3-backend`, `4-agents`, `5-frontend`, `6-channels`).

- **Data:** a slot's databases start empty (all migrations applied) and live until `just down`.
- **Migrations:** `just restart` applies a new migration (it stops and starts the slot on its
  databases). After editing an already-applied one, the slot refuses to start; `just db-reset`
  rebuilds the databases.
- **A crashed service** stays exited: `just slots` shows the slot as `failed`, and `just up` names the
  process. Fix it and `just restart` (or restart just that process, above).
- **When you are done:** `just down` (stops it, drops its databases, frees the ports). `just slots`
  lists every running stack, slot 0 included, with its state, branch and worktree; `just slots-gc`
  releases slots whose stack is gone.
- **One slot per worktree:** agents working in the same worktree share its slot.
- **Slot 0 is the user's** `just start-local` (console `:5173`, process-compose TUI, interactive). Only
  slot 0 runs the Slack and Google Chat clients; for channel work, ask the user rather than starting
  it yourself. `stop-local` / `reset-local` take the shared PostgreSQL and Keycloak away from every
  slot — do not run them.

**The env files are gitignored and per-checkout** — they exist in the main checkout, not in a fresh worktree. `just up` and `just start-local` first run `just env-sync`, which copies the files listed in `scripts/local-dev/worktree-env-files` (`.env`, `litellm-local-models.yaml`, `packages/client-slack/.env`, the orchestrator's `tests/integration/.env.integration`) from the main checkout when the worktree lacks them; it never overwrites one you changed (`just env-sync --force` refreshes them). If the main checkout has no `.env` either, or the stack reports no LLM provider / missing config, **STOP and ask the user to provide it — never fabricate secrets, AWS profiles, or OIDC URLs.** Ask only for the *minimal subset the task needs*, not the whole file. Variables group by what they unlock:

| Var(s) | Unlocks |
|--------|---------|
| `AWS_PROFILE`, `OIDC_ISSUER`, `MCP_GATEWAY_URL` | **Minimum** to boot the stack with cloud models + remote auth + tools |
| `LANGSMITH_ORGANIZATION_ID`, `LANGSMITH_PROJECT_ID` | Tracing + usage links |
| `GATANA_API_KEY`, `GATANA_ORG_ID`, `SANDBOX_PROVIDER`, `SANDBOX_POOL_CAPACITY`, `SANDBOX_WARM_TTL` | Sandbox-related tests |
| `CODE_INTERPRETER_PTC=1` | In-process code-interpreter (PTC) |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | GitHub-backed skill registry |

So e.g. a UI change touching only the console needs just the **Minimum** rows; a sandbox/code-interpreter change additionally needs the Gatana + PTC rows.

**End-to-end code review (incl. QA):** when reviewing or verifying a change that affects runtime behavior, don't stop at reading the diff — bring up your slot (`just up`, which prints its URLs), then exercise the change in the browser. For frontend/UI behavior, drive live QA against your slot's console URL (e.g. the `frontend-qa-chrome-observer` agent, or `@browser` directly) to capture screenshots / console / network evidence before sign-off.

**Verifying server-to-server / log-only effects:** some behaviors aren't visible in the browser's network panel because they originate server-side. Verify these in your slot's `~/.nannos/slots/N/logs/<service>.log` (slot 0: `logs/<service>.log`). For the discovery cache specifically: run a chat turn for the user so their cache is populated (`orchestrator.log`: `[DISCOVERY-CACHE] miss → discovered … for user_sub=…`), trigger an entitlement change in the Console (e.g. enable a sub-agent for the orchestrator), then run another turn and expect a fresh `[DISCOVERY-CACHE] miss` (the entitlement version moved, so the old key is unreachable); an unchanged entitlement yields `[DISCOVERY-CACHE] hit`. A `[USER-STAMPS] fetch failed … reusing last known stamps` warning means console-backend was unreachable for the per-turn stamp fetch and the turn fell back to the TTL-bounded entry.

### K8s Deployment

Manifests in `example-k8s-deployment/base/`. Uses Kustomize with overlays for image patching and secrets. Gateway HTTPRoutes expose `orchestrator.<DOMAIN>`, `console.<DOMAIN>/api`, `console.<DOMAIN>/`.

## Skills

- **add-package** (`.github/skills/add-package/SKILL.md`): Checklist and procedure for adding a new package to the monorepo — covers directory setup, release-helpers.sh, justfile, and optional k8s manifests.
- **deploy** (`.github/skills/deploy/SKILL.md`): Deploy a package to dev via FluxCD — covers `just deploy-dev`, gitops symlink requirements, Flux image automation with `-next` tag filtering, and troubleshooting.
- **embed-sdk-hosts** (`.github/skills/embed-sdk-hosts/SKILL.md`): Develop and release `@nannos/embed-sdk` together with the apps that install it — covers the `hosts/` symlink registry, `just hosts` / `host-link` / `host-unlink` / `host-bump`, npm publishing inside `just release`, and reading `just hosts` verdicts.
