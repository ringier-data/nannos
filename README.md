# Nannos

An enterprise-grade multi-agent AI platform built on the [Agent-to-Agent (A2A) protocol](https://google.github.io/A2A/). Users interact through web or messaging clients; a central orchestrator plans tasks and delegates work to specialized sub-agents.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│  Clients                                                                         │
│  ┌──────────────────┐   ┌──────────────┐   ┌───────────────┐  ┌───────────────┐  │
│  │  Console Frontend│   │ Client Slack │   │ Client Email  │  │ Google Chat   │  │
│  └────────┬─────────┘   └──────┬───────┘   └──────┬────────┘  └──────┬────────┘  │
└───────────┼────────────────────┼──────────────────┼──────────────────┼───────────┘
            │ REST / WS          │ A2A              │ A2A              │ A2A
            ▼                    ▼                  ▼                  ▼
┌───────────────────┐A2A ┌────────────────────────────────────────────────┐
│  Console Backend  │◄─--│                  Orchestrator Agent            │
│  (API + Scheduler)│    │            (LangGraph — plans & delegates)     │
└───────────────────┘    └──────────────────────────┬─────────────────────┘
                                                    │ A2A    
                                          ┌─────────▼───┐  
                                          │ Sub Agents  │   
                                          └─────────────┘ 

```

## Quick-Start Local

```bash
export OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234
just start-local
```

---

## Deployment Scopes
<table>
<tr><td><b>Scope</b></td><td><b>Components</b></td><td><b>External Dependencies</b></td></tr>
<tr>
  <td valign="top">PoC</td>
  <td valign="top">• Orchestrator (low capability)</td>
  <td valign="top">
    • OIDC Issuer<br>
    • Orchestrator OIDC Client<br>
    • LLM Provider (local or remote)<br>
    • PostgresSQL (+pgvector)
  </td>
</tr>
<tr>
  <td valign="top">Medium</td>
  <td valign="top">• Orchestrator</td>
  <td valign="top">
    + S3, PostgreSQL (LangGraph Checkpointing)<br>
    + S3 (binary file storage)<br>
    + MCP Gateway (e.g. <a href="https://www.gatana.ai">Gatana</a>)
  </td>
</tr>
<tr>
  <td valign="top">Full</td>
  <td valign="top">• Orchestrator<br>• Agent Runner<br>• Console (Web admin app)</td>
  <td valign="top">
    + PostgreSQL pgcrypto extension<br>
    + Console OIDC Client<br>
    + KeyCloak Admin Credentials
  </td>
</tr>
<tr>
  <td valign="top">+ Slack</td>
  <td valign="top">
    • Slack Client<br>
    • Slack Client Admin GUI<br>
  </td>
  <td valign="top">
    + Slack Client OIDC Client<br>
  </td>
</tr>
<tr>
  <td valign="top">+ Email</td>
  <td valign="top">
    • Email Client
  </td>
  <td valign="top">
    + Email Client OIDC Client<br>
    + AWS SES
  </td>
</tr>
<tr>
  <td valign="top">+ Google Chat</td>
  <td valign="top">
    • Google Chat Client
  </td>
  <td valign="top">
    + Google Chat Client OIDC Client<br>
    + GCP Service Account
  </td>
</tr>
</table>

## External Dependencies

### Model Provider

All LLM traffic routes through the **Model Gateway** (a LiteLLM proxy) — it is the single source of truth for which models exist. Services (orchestrator and any A2A agent built on `agent-common`) talk only to the gateway; they hold **no** provider credentials of their own:

| Variable | Description |
| - | - |
| `LLM_GATEWAY_URL` | URL of the LiteLLM proxy (e.g. `http://litellm-proxy:4000`) |
| `LLM_GATEWAY_API_KEY` | Virtual key the service authenticates to the gateway with |

Provider credentials live on the **gateway**, where each model is registered in its `model_list`:

| Provider | Configuration (on the gateway) |
| - | - |
| Local / OpenAI-compatible (Ollama, LM Studio, vLLM, …) | `model: openai/<name>`, `api_base`, `api_key` |
| AWS Bedrock | `model: bedrock/…` + AWS SDK environment variables |
| Azure OpenAI | `model: azure/…` + `AZURE_OPENAI_API_KEY` / `AZURE_API_BASE` |
| Google Vertex | `model: vertex_ai/…` + `GCP_KEY` / `GCP_PROJECT_ID` |

For local development, `scripts/start-local.sh` provisions the gateway for you: set `OPENAI_COMPATIBLE_BASE_URL` (and optionally `OPENAI_COMPATIBLE_MODEL` / `OPENAI_COMPATIBLE_API_KEY`) and it registers your local server as the gateway `local` model — see [Quick-Start Local](#quick-start-local).

### Tracing 

You can enable tracing by setting
* LANGSMITH_API_KEY
* LANGSMITH_TRACING = true
* LANGSMITH_ENDPOINT
* LANGSMITH_PROJECT

### OIDC Issuer

We recommend KeyCloak, as the Console has connector for managing users and groups in KeyCloak from within the Console frontend app.

### PostgreSQL

Full scope extensions required: pgcrypto and pgvector.

The following components executes SQL DB migrations on start-up and we recommend to prepare an empty schema for each:
* Orchestrator (suggested username `docstore` since its used for semantic search over documents)
* Console Backend
* Slack Client
* Email Client
* Google Chat Client

| Component | PG database/schema | Shared | Need Schema Owner |
| - | - | - |- |
| Orchestrator | docstore | ✅ | ✅ |
| Agent Runner | docstore | ✅ |  |
| Any A2A Server based on agent-common | docstore | ✅ | |
| Console | console | | ✅ |
| Slack Client | slack client  | | ✅ |
| Email Client | email client | | ✅ |
| Google Chat Client | google chat client | | ✅ |


## Components
### Required

| Image | Purpose | Port |
|-------|---------|:---:|
| `ghcr.io/ringier-data/nannos-orchestrator-agent` | **Required: entry-point for requests.** A2A server powered by LangGraph. Receives tasks, plans execution, discovers sub-agents, and delegates work. | 10001 |
| `ghcr.io/ringier-data/nannos-litellm-proxy` | **Required: the Model Gateway.** LiteLLM proxy through which all services make LLM/embedding calls; single source of truth for the model registry and cost tracking. | 4000 |

### Core
| Image | Purpose | Port |
|-------|---------|:---:|
| `ghcr.io/ringier-data/nannos-agent-runner` | **Required for scheduled jobs** A2A server that executes scheduled automated sub-agent jobs. | 5005 |
| `ghcr.io/ringier-data/nannos-console-backend` | **Admin Management** REST + WebSocket API. Manages agents, conversations, file uploads, and the background scheduler. The single backend all other services register against. | 5001 |
| `ghcr.io/ringier-data/nannos-console-frontend` | **Admin Management**  React SPA served by nginx. The primary operator/user web interface. | 8081 |

### A2A Clients
| Image | Purpose | Port |
|-------|---------|:---:|
| `ghcr.io/ringier-data/nannos-client-slack` | Slack A2A Client | 3000 |
| `ghcr.io/ringier-data/nannos-client-slack-frontend` | Slack Admin Management Frontend (React SPA) | 8080 |
| `ghcr.io/ringier-data/nannos-client-email` | Email A2A Client | 3000 |
| `ghcr.io/ringier-data/nannos-client-google-chat` | Google Chat A2A Client | 3000 |

### Sub-Agents
| Image | Purpose | Port |
|-------|---------|:---:|
| `ghcr.io/ringier-data/nannos-agent-creator` | A2A server for designing and generating new agents via LLM. | 8080 |

---

## Required Configuration

### `nannos-orchestrator-agent`

Full reference: [`packages/orchestrator-agent/`](packages/orchestrator-agent/)

| Variable | Description |
|----------|-------------|
| `OIDC_ISSUER` | OIDC issuer URL |
| `AGENT_BASE_URL` | Public URL of this service, used in the A2A agent card (default: `http://localhost:10001`) |
| `CONSOLE_BACKEND_URL` | URL of the console-backend service (default: `http://localhost:5001`) |
| `POSTGRES_HOST` | PostgreSQL hostname |
| `POSTGRES_DB` | PostgreSQL database |
| `POSTGRES_USER` | docstore user |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_SCHEMA` | Schema name (required for migrations to work) |

**Model Gateway — required (the sole path for LLM calls):**

| Variable | Description |
|----------|-------------|
| `LLM_GATEWAY_URL` | URL of the LiteLLM proxy (e.g. `http://litellm-proxy:4000`) |
| `LLM_GATEWAY_API_KEY` | Virtual key for authenticating to the gateway |

Provider credentials (Bedrock/Azure/Vertex/local) are configured on the gateway, not here — see [Model Provider](#model-provider).


---

### `nannos-console-backend`

Full reference: [`packages/console-backend/.env.template`](packages/console-backend/.env.template)

| Variable | Description |
|----------|-------------|
| `OIDC_ISSUER` | OIDC issuer URL |
| `OIDC_CLIENT_ID` | Keycloak client ID for this service (e.g. `agent-console`) |
| `OIDC_CLIENT_SECRET` | Client secret |
| `BASE_DOMAIN` | Public domain used for CORS origin checks |
| `SECRET_KEY` | Secret used to sign sessions — **change this in production** |
| `ORCHESTRATOR_CLIENT_ID` | Keycloak client ID of the orchestrator (e.g. `orchestrator`) |
| `ORCHESTRATOR_BASE_DOMAIN` | e.g. `orchestrator.nannos.mydomain.com` |
| `POSTGRES_HOST` | PostgreSQL hostname |
| `POSTGRES_PORT` | PostgreSQL port (default: `5432`) |
| `POSTGRES_DB` | Database name |
| `POSTGRES_USER` | Database user |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_SCHEMA` | Schema name (required for migrations to work) |
| `LANGSMITH_ORGANIZATION_ID` | LangSmith organization ID for frontend trace links (optional) |
| `LANGSMITH_PROJECT_ID` | LangSmith project ID for frontend trace links (optional) |
| `AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH` | Max chars for auto-approve system prompt (default: `500`) |
| `AUTO_APPROVE_MAX_MCP_TOOLS_COUNT` | Max MCP tools for auto-approve (default: `3`) |

---

### `nannos-agent-creator`

Full reference: [`packages/agent-creator/.env.template`](packages/agent-creator/.env.template)

| Variable | Description |
|----------|-------------|
| `OIDC_ISSUER` | OIDC issuer URL |
| `OIDC_CLIENT_ID` | Keycloak client ID (e.g. `agent-creator`) |
| `OIDC_CLIENT_SECRET` | Client secret |
| `ORCHESTRATOR_CLIENT_ID` | Keycloak client ID used to validate inbound tokens |
| `AGENT_ID` | Numeric identifier for this agent instance (e.g. `1`) |
| `CONSOLE_BACKEND_URL` | URL of the console-backend service (default: `http://localhost:5001`) |
| `CONSOLE_FRONTEND_URL` | URL of the console-frontend (default: `http://localhost:5173`) |

Model Gateway — same as the orchestrator: set `LLM_GATEWAY_URL` and `LLM_GATEWAY_API_KEY`.

---

### `nannos-agent-runner`

Full reference: [`packages/agent-runner/.env.template`](packages/agent-runner/.env.template)

| Variable | Description |
|----------|-------------|
| `OIDC_ISSUER` | OIDC issuer URL |
| `OIDC_CLIENT_ID` | Keycloak client ID (uses `orchestrator` client) |
| `AGENT_BASE_URL` | Public URL of this service (default: `http://localhost:5005`) |
| `CONSOLE_BACKEND_URL` | URL of the console-backend service (default: `http://localhost:5001`) |
| `POSTGRES_HOST` | PostgreSQL hostname (for document store) |
| `POSTGRES_PORT` | PostgreSQL port (default: `5432`) |
| `POSTGRES_DB` | PostgreSQL database |
| `POSTGRES_USER` | docstore user |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_SCHEMA` | Schema name (required for migrations to work) |

Model Gateway — same as the orchestrator: set `LLM_GATEWAY_URL` and `LLM_GATEWAY_API_KEY`.

---

### `nannos-client-slack`

Full reference: [`packages/client-slack/.env.example`](packages/client-slack/.env.example)

All variables below are **required** — there are no fallback defaults.

| Variable | Description |
|----------|-------------|
| `ENVIRONMENT` | Deployment environment: `local`, `dev`, `stg`, or `prod` |
| `OIDC_ISSUER_URL` | OIDC issuer URL |
| `OIDC_CLIENT_ID` | Keycloak client ID for this service |
| `OIDC_CLIENT_SECRET` | Client secret |
| `A2A_SERVER_URL` | Full URL of the orchestrator A2A endpoint, e.g. `https://orchestrator.example.com/api` |
| `ADMIN_GROUP` | Keycloak group name that grants admin access |
| `POSTGRES_HOST` | PostgreSQL hostname |
| `POSTGRES_PASSWORD` | Postgres password |
| `POSTGRES_USER` | Postgres user |
| `POSTGRES_DB` | Postgres database |

---

### `nannos-client-email`

Full reference: [`packages/client-email/.env.example`](packages/client-email/.env.example)

| Variable | Description |
|----------|-------------|
| `ENVIRONMENT` | Deployment environment: `local`, `dev`, `stg`, or `prod` |
| `OIDC_ISSUER_URL` | OIDC issuer URL |
| `OIDC_CLIENT_ID` | Keycloak client ID for this service |
| `OIDC_CLIENT_SECRET` | Client secret |
| `A2A_SERVER_URL` | Full URL of the orchestrator A2A endpoint |

---

### `nannos-client-google-chat`

Full reference: [`packages/client-google-chat/.env.example`](packages/client-google-chat/.env.example)

| Variable | Description |
|----------|-------------|
| `ENVIRONMENT` | Deployment environment: `local`, `dev`, `stg`, or `prod` |
| `BASE_URL` | Google chat serve url |
| `OIDC_ISSUER_URL` | OIDC issuer URL |
| `OIDC_CLIENT_ID` | Keycloak client ID for this service |
| `OIDC_CLIENT_SECRET` | Client secret |
| `A2A_SERVER_URL` | Full URL of the orchestrator A2A endpoint |
| `GCP_CHAT_PROJECTS` | JSON array of Google Chat project configs |
| `GCP_SA_JSON_KEY_*` | GCP service account key JSON (one per project) |
| `GOOGLE_CHAT_TOKEN_EXPECTED_AUDIENCE` | Google chat expected audience in events token |
| `POSTGRES_HOST` | PostgreSQL hostname |
| `POSTGRES_PASSWORD` | Postgres password |
| `POSTGRES_USER` | Postgres user |
| `POSTGRES_DB` | Postgres database |

---

### `nannos-console-frontend`

The frontend is a static SPA served by nginx. It requires **no build-time environment variables** —
all configuration is loaded at runtime from the console-backend via `GET /api/v1/config`.

---

### `nannos-client-slack-frontend` (build-time)

| Variable | Description |
|----------|-------------|
| `OVERRIDE_OPENAPI_URL` | URL to the Slack backend's OpenAPI spec, used for client code generation at build time (e.g. `http://client-slack:3001/api/v2/openapi.json`) |

---
