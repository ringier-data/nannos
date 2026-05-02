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
| `agent-common` | Python | Lib | — | LLM model factory (OpenAI/Bedrock/Azure/Google), LangGraph checkpoints, MCP adapters. Consumed by all Python agents |
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
- **LLM abstraction**: `agent-common` provides a unified model factory — switch providers (Bedrock, Azure, OpenAI, Google) via env vars without code changes.
- **Stateless services**: All conversation/checkpoint state persists in DynamoDB + PostgreSQL, enabling horizontal scaling.
- **MCP for tools**: Model Context Protocol pattern allows dynamic tool discovery and composition at runtime.
- **Multi-stage Docker builds**: Python services use `uv` for fast cached installs; shared libs (`ringier-a2a-sdk`, `agent-common`) are passed as Docker build contexts.

### Infrastructure Requirements

- **PostgreSQL**: `console` schema (pgcrypto), `docstore` schema (pgvector) shared by agents, `slack_client`, `email_client`, and `google_chat_client` schemas
- **Auth**: Keycloak (or any OIDC provider). Per-service OIDC clients: `orchestrator`, `agent-console`, `slack-client`, `email-client`, `google-chat-client`
- **AWS** (production): DynamoDB (checkpoints), S3 (files), SES (email), Secrets Manager, SSM
- **LLM providers**: Local (OpenAI-compatible), AWS Bedrock, Azure OpenAI, Google Vertex AI — configurable via env vars
- **Optional**: MCP Gateway (tool extensions), LangSmith (tracing)

### Directory Layout Patterns

**Python services** follow: `main.py` (entry) → `app/` or `agent/` → `core/` (agent, executor, graph) + `models/` + `middleware/` + `handlers/`

**Node services** follow: `src/app.ts` (entry) → `config/` + `services/` + `storage/` + `controllers/` + `middleware/`

**Frontend SPAs** follow: `src/main.tsx` → `pages/` + `components/` + `api/generated/` + `hooks/` + `contexts/`

### Local Development

Run `scripts/start-local.sh` — provides 3 scenarios:
1. **Full local**: Local LLM + local Keycloak
2. **Local + AWS**: Local LLM + cloud models (Bedrock/Azure/GCP) + local Keycloak
3. **Local + AWS + Remote OIDC**: Cloud models + production Keycloak

Starts PostgreSQL (ports 5401/5402), Keycloak (8180 if local), runs DB migrations, launches services in tmux sessions.

### K8s Deployment

Manifests in `example-k8s-deployment/base/`. Uses Kustomize with overlays for image patching and secrets. Gateway HTTPRoutes expose `orchestrator.<DOMAIN>`, `console.<DOMAIN>/api`, `console.<DOMAIN>/`.

## Skills

- **add-package** (`.github/skills/add-package/SKILL.md`): Checklist and procedure for adding a new package to the monorepo — covers directory setup, release-helpers.sh, justfile, and optional k8s manifests.
- **deploy** (`.github/skills/deploy/SKILL.md`): Deploy a package to dev via FluxCD — covers `just deploy-dev`, gitops symlink requirements, Flux image automation with `-next` tag filtering, and troubleshooting.
