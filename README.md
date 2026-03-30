# Nannos

An enterprise-grade multi-agent AI platform built on the [Agent-to-Agent (A2A) protocol](https://google.github.io/A2A/). Users interact through web or messaging clients; a central orchestrator plans tasks and delegates work to specialized sub-agents.

```
┌─────────────────────────────────────────────────────────────────┐
│  Clients                                                        │
│  ┌──────────────────┐   ┌──────────────┐   ┌───────────────┐    │
│  │  Console Frontend│   │ Client Slack │   │ Client Email  │    │
│  └────────┬─────────┘   └──────┬───────┘   └──────┬────────┘    │
└───────────┼────────────────────┼──────────────────┼─────────────┘
            │ REST / WS          │ A2A              │ A2A
            ▼                   ▼                 ▼
┌───────────────────┐A2A ┌──────────────────────────────────────┐
│  Console Backend  │◄─--│          Orchestrator Agent          │
│  (API + Scheduler)│    │  (LangGraph — plans & delegates)     │
└───────────────────┘    └──────────────┬───────────────────────┘
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
    + S3, DynamoDB (LangGraph Checkpointing)<br>
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
</table>

## External Dependencies

### OIDC Issuer

We recommend KeyCloak, as the Console has connector for managing users and groups in KeyCloak from within the Console frontend app.

### PostgreSQL

Full scope extensions required: pgcrypto and pgvector.

The following components executes SQL DB migrations on start-up and we recommend to prepare an empty schema for each:
* Orchestrator (suggested username `docstore` since its used for semantic search over documents)
* Console Backend
* Slack Client
* Email Client

| Component | PG database/schema | Shared | Need Schema Owner |
| - | - | - |- |
| Orchestrator | docstore | ✅ | ✅ |
| Agent Runner | docstore | ✅ |  |
| Any A2A Server based on agent-common | docstore | ✅ | |
| Console | console | | ✅ |
| Slack Client | slack client  | | ✅ |
| Email Client | email client | | ✅ |


## Components
### Required

| Image | Purpose | Port |
|-------|---------|:---:|
| `ghcr.io/ringier-data/nannos-orchestrator-agent` | **Required: entry-point for requests.** A2A server powered by LangGraph. Receives tasks, plans execution, discovers sub-agents, and delegates work. | 10001 |

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
| `PLAYGROUND_BACKEND_URL` | URL of the console-backend service (default: `http://localhost:5001`) |
| `POSTGRES_HOST` | PostgreSQL hostname |
| `POSTGRES_DB` | PostgreSQL database |
| `POSTGRES_USER` | docstore user |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_SCHEMA` | Schema name (required for migrations to work) |

**LLM provider — provide credentials for at least one:**

| Variable | Provider |
|----------|----------|
| `OPENAI_COMPATIBLE_BASE_URL` | Any OpenAI-compatible server (Ollama, LM Studio, vLLM, etc.) |
| `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + `AWS_BEDROCK_REGION` | Amazon Bedrock |
| `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` + `OPENAI_API_VERSION` | Azure OpenAI |


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
| `PLAYGROUND_BACKEND_URL` | URL of the console-backend service (default: `http://localhost:5001`) |
| `PLAYGROUND_FRONTEND_URL` | URL of the console-frontend (default: `http://localhost:5173`) |

LLM provider credentials — same options as the orchestrator.

---

### `nannos-agent-runner`

Full reference: [`packages/agent-runner/.env.template`](packages/agent-runner/.env.template)

| Variable | Description |
|----------|-------------|
| `OIDC_ISSUER` | OIDC issuer URL |
| `OIDC_CLIENT_ID` | Keycloak client ID (uses `orchestrator` client) |
| `AGENT_BASE_URL` | Public URL of this service (default: `http://localhost:5005`) |
| `PLAYGROUND_BACKEND_URL` | URL of the console-backend service (default: `http://localhost:5001`) |
| `POSTGRES_HOST` | PostgreSQL hostname (for document store) |
| `POSTGRES_PORT` | PostgreSQL port (default: `5432`) |
| `POSTGRES_DB` | PostgreSQL database |
| `POSTGRES_USER` | docstore user |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_SCHEMA` | Schema name (required for migrations to work) |

LLM provider credentials — same options as the orchestrator.

---

### `nannos-client-slack`

Full reference: [`packages/client-slack/.env.example`](packages/client-slack/.env.example)

All variables below are **required** — there are no fallback defaults.

| Variable | Description |
|----------|-------------|
| `ENVIRONMENT` | Deployment environment: `local`, `dev`, `stg`, or `prod` |
| `SLACK_SIGNING_SECRET` | Slack app signing secret |
| `SLACK_CLIENT_ID` | Slack OAuth client ID |
| `SLACK_CLIENT_SECRET` | Slack OAuth client secret |
| `SLACK_STATE_SECRET` | OAuth state secret (any random string) |
| `SLACK_APP_TOKEN` | App-level token (`xapp-…`) for Socket Mode |
| `OIDC_ISSUER_URL` | OIDC issuer URL |
| `OIDC_CLIENT_ID` | Keycloak client ID for this service |
| `OIDC_CLIENT_SECRET` | Client secret |
| `A2A_SERVER_URL` | Full URL of the orchestrator A2A endpoint, e.g. `https://orchestrator.example.com/api` |
| `ADMIN_GROUP` | Keycloak group name that grants admin access |
| `POSTGRES_HOST` | PostgreSQL hostname |

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

### `nannos-console-frontend` (build-time)

These variables must be set **at image build time** (Vite bakes them into the bundle).

| Variable | Description | Default |
|----------|-------------|---------|
| `VITE_API_BASE_URL` | URL of the console-backend API | `http://localhost:5001` |
| `VITE_KEYCLOAK_BASE_URL` | Keycloak base URL | `https://login.p.nannos.rcplus.io` |
| `VITE_KEYCLOAK_REALM` | Keycloak realm name | `nannos` |
| `VITE_ORCHESTRATOR_BASE_DOMAIN` | Orchestrator host | `orchestrator.d.nannos.rcplus.io` |

---

### `nannos-client-slack-frontend` (build-time)

| Variable | Description |
|----------|-------------|
| `OVERRIDE_OPENAPI_URL` | URL to the Slack backend's OpenAPI spec, used for client code generation at build time (e.g. `http://client-slack:3001/api/v2/openapi.json`) |

---
