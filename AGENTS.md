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
Sub-Agents (agent-creator, agent-runner, user-created agents)
```

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
  - **Cost Attribution** — who to bill (user / sub-agent / conversation / scheduled job); travels to the proxy per-request as `spend_logs_metadata`.
- **Reasoning effort**: extended thinking uses LiteLLM's unified `reasoning_effort`; the app keeps only a small `thinking_level → reasoning_effort` map (budgets are provider-determined).
- **Streaming watchdog**: a mandatory client-side inter-chunk watchdog bounds streaming, because the proxy silently ignores `stream_timeout` on Bedrock streaming (LiteLLM #23375); proxy timeouts are a best-effort outer bound only.
- **Stateless services**: All conversation/checkpoint state persists in DynamoDB + PostgreSQL, enabling horizontal scaling.
- **MCP for tools**: Model Context Protocol pattern allows dynamic tool discovery and composition at runtime.
- **Multi-stage Docker builds**: Python services use `uv` for fast cached installs; shared libs (`ringier-a2a-sdk`, `agent-common`) are passed as Docker build contexts.
- **Skills registry as source of truth**: All skill content lives in `skill_registry` table. Docstore is a runtime cache. Activations are content-hash pinned for version control.
- **HITL-guarded self-improvement**: All skill/playbook mutations require user approval via `HumanInTheLoopMiddleware` interrupt. Agents propose, users approve/edit/reject.
- **Sandbox per A2A turn**: Sandbox-enabled agents acquire a fresh sandbox per invocation (not per session) via `SandboxPool`. Sandboxes are warm-cached by `(session_id, sub_agent_name)`.
- **Single graph per model type**: The orchestrator uses ONE compiled graph per model, shared across all users. Tools are injected at runtime via `GraphRuntimeContext`, not baked in.
- **Per-user discovery/registry cache + scoped invalidation**: The orchestrator memoizes capability discovery (MCP tools + sub-agents) and the registry user lookup per user (`app/core/discovery_cache.py`), keyed by `cache_key(user_sub, groups, sub_agent_config_hash, policy_version)`, TTL-bounded and additionally bounded by the user token's `exp`. When an entitlement changes (group default-agents, MCP-gateway server access, role, tool whitelist, bypass rules), console-backend POSTs `/internal/discovery-cache/invalidate` **scoped to the affected `user_subs`** (group members, or a single user) — not a global flush — authenticated service-to-service via client-credentials and gated on the caller's `azp == CONSOLE_BACKEND_CLIENT_ID`. Invalidation is best-effort and dispatched as a background task (never blocks the triggering request); the in-process cache is per-replica, so TTL is the multi-replica correctness floor. **Scoping vocabulary is the current OIDC `sub`** (`user.sub`), not the stable `user.id` PK — these differ for pre-existing users.

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

**Before starting anything, check whether the stack is already running** — `start-local` is slow and a running stack is the common case. Probe the well-known local ports and reuse them if up:

```bash
# NB: no `-f` — the backends answer non-2xx on `/` even when healthy (console-backend :5001 → 404,
# orchestrator :10001 → 401 behind its JWT middleware), so `-f` would report them falsely "down".
# This checks "is the port answering HTTP at all".
for p in 5173 5001 10001 8180; do curl -s -o /dev/null --max-time 2 "http://localhost:$p" && echo "✓ :$p up" || echo "✗ :$p down"; done
```

Local URLs/ports: Console `http://localhost:5173`, Backend API `:5001`, Orchestrator `:10001`, Keycloak `:8180`, PostgreSQL `:5401` (console) / `:5402` (docstore + checkpoints).

**Starting the stack:** `just start-local` (→ `scripts/start-local.sh`) sources `.env` from the repo root, starts PostgreSQL + Keycloak, runs DB migrations, and launches every service via **mprocs** (services hot-reload on edit; `Ctrl+C`/`q` in mprocs stops everything; per-service logs in `logs/<service>.log`). Three scenarios, driven by env:
1. **Full local**: `OPENAI_COMPATIBLE_BASE_URL` set → local LLM + local Keycloak
2. **Local + AWS**: `AWS_PROFILE` set → cloud models (Bedrock/Azure/GCP) + local Keycloak
3. **Local + AWS + Remote OIDC**: `AWS_PROFILE` + `OIDC_ISSUER` set → cloud models + remote Keycloak (skips local Keycloak)

**Starting it non-interactively (from an agent):** the script execs **mprocs** (a TUI) and has a `Proceed? [Y/n]` prompt, so it needs a real TTY — piping `yes |` fails with `Error: Stdin is not a tty`. Run it inside a detached tmux session and answer the prompt via send-keys, then poll for readiness:
```bash
aws sso login --profile "$AWS_PROFILE"   # if using AWS; SSO sessions expire
tmux new-session -d -s nannos -x 220 -y 50 "./scripts/start-local.sh"
sleep 4 && tmux send-keys -t nannos "y" Enter            # answer Proceed?
# wait ~30s for frontend, ~2-3 min for full stack; inspect with: tmux capture-pane -t nannos -p
```
Service status is the left column of `tmux capture-pane -t nannos -p` (`UP`/`DOWN`).

**The `.env` file is gitignored and per-checkout** — it exists at the main repo root but **NOT in fresh git worktrees**. If `scripts/start-local.sh` reports no LLM provider / missing config, or `.env` is absent, **STOP and ask the user to provide it — never fabricate secrets, AWS profiles, or OIDC URLs.** Ask only for the *minimal subset the task needs*, not the whole file. Variables group by what they unlock:

| Var(s) | Unlocks |
|--------|---------|
| `AWS_PROFILE`, `OIDC_ISSUER`, `MCP_GATEWAY_URL` | **Minimum** to boot the stack with cloud models + remote auth + tools |
| `LANGSMITH_ORGANIZATION_ID`, `LANGSMITH_PROJECT_ID` | Tracing + usage links |
| `GATANA_API_KEY`, `GATANA_ORG_ID`, `SANDBOX_PROVIDER`, `SANDBOX_POOL_CAPACITY`, `SANDBOX_WARM_TTL` | Sandbox-related tests |
| `CODE_INTERPRETER_PTC=1` | In-process code-interpreter (PTC) |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | GitHub-backed skill registry |

So e.g. a UI change touching only the console needs just the **Minimum** rows; a sandbox/code-interpreter change additionally needs the Gatana + PTC rows.

**End-to-end code review (incl. QA):** when reviewing or verifying a change that affects runtime behavior, don't stop at reading the diff — confirm the relevant service is up (probe above; if not, follow the start/`.env` steps), then exercise the change in the browser. For frontend/UI behavior, drive live QA against `http://localhost:5173` (e.g. the `frontend-qa-chrome-observer` agent, or `@browser` directly) to capture screenshots / console / network evidence before sign-off.

**Verifying server-to-server / log-only effects:** some behaviors aren't visible in the browser's network panel because they originate server-side (e.g. the console-backend → orchestrator discovery-cache invalidation POST). Verify these in `logs/<service>.log`. For the discovery cache specifically: trigger an entitlement change in the Console, then check `console-backend.log` for `Invalidated orchestrator discovery cache (…; scope=N user(s))` and `orchestrator.log` for `Scoped discovery-cache invalidation: N users, M entries dropped`. To see `M > 0` (entries actually dropped), first run a chat turn for the user so their cache is populated (`[DISCOVERY-CACHE] miss → discovered … for user_sub=…`), *then* trigger the invalidation. A scoped flush only matches when the invalidated `sub` equals the `user_sub` the orchestrator cached under (current OIDC sub).

### K8s Deployment

Manifests in `example-k8s-deployment/base/`. Uses Kustomize with overlays for image patching and secrets. Gateway HTTPRoutes expose `orchestrator.<DOMAIN>`, `console.<DOMAIN>/api`, `console.<DOMAIN>/`.

## Skills

- **add-package** (`.github/skills/add-package/SKILL.md`): Checklist and procedure for adding a new package to the monorepo — covers directory setup, release-helpers.sh, justfile, and optional k8s manifests.
- **deploy** (`.github/skills/deploy/SKILL.md`): Deploy a package to dev via FluxCD — covers `just deploy-dev`, gitops symlink requirements, Flux image automation with `-next` tag filtering, and troubleshooting.
