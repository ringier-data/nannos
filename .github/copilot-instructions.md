# Alloy Infrastructure Agents - Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Common Conventions

- **Python Commands**: Always use `uv` for all Python operations (`uv sync`, `uv run pytest`, `uv run python`)
- **Testing**: Prefer the runTests MCP tool over terminal commands when running tests
- **File Writing**: NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits instead.

## Repository Overview

This is a monorepo containing multiple services and libraries for the Alloy Infrastructure Agents project:

- **playground-backend**: FastAPI backend service with PostgreSQL, DynamoDB, and authentication
- **playground-frontend**: React/TypeScript frontend with Vite
- **orchestrator-agent**: LangGraph-based agent orchestration service with document store
- **agent-creator**: A2A Server for creating and managing sub-agents
- **ringier-a2a-sdk**: Python SDK for Agent-to-Agent communication
- **infrastructure**: Ansible playbooks for deployment


## Deployment & Infrastructure as Code

All infrastructure is managed as code using Ansible playbooks and CloudFormation templates located in `/infrastructure`.

### Infrastructure Roles

Each component has a dedicated Ansible role in `infrastructure/roles/`:

- **basis**: Core infrastructure setup
  - PostgreSQL database provisioning and schema migrations
  - Network configuration and security groups
  - Base system packages and configuration
  
- **playground-backend**: Playground Backend service deployment
  - FastAPI application deployment
  - PostgreSQL connection configuration
  - DynamoDB tables for sessions and conversations
  - Application secrets from AWS SSM
  
- **orchestrator-agent**: Orchestrator Agent service deployment
  - LangGraph agent service deployment
  - PostgreSQL with pgvector for document store
  - DynamoDB checkpointers
  - S3 bucket configuration for checkpoint storage
  
- **agent-creator**: Agent Creator service deployment
  - A2A server deployment
  - DynamoDB and S3 for checkpoints
  - Integration with Playground Backend
  
- **playground-frontend**: Frontend application deployment
  - React/Vite static site deployment
  - Environment-specific configuration
  - Backend API endpoint configuration

### Deployment Process

- CloudFormation stacks define AWS resources (RDS, DynamoDB, S3, etc.)
- Ansible playbooks orchestrate provisioning and deployment
- Service-specific roles handle application deployment and configuration
- All secrets are managed via AWS SSM Parameter Store
- Migrations run automatically via Rambler during basis role execution

## Local Development Environment

**CRITICAL: The `/start-dev.sh` script is the single source of truth for local environment setup**

### When to Update start-dev.sh

Any changes that impact how services run locally MUST be reflected in `start-dev.sh`:

- **New environment variables**: Add to SSM fetching or default values
- **New secrets/credentials**: Add AWS SSM parameter fetching
- **Configuration changes**: Update .env generation logic
- **New service dependencies**: Add startup requirements
- **Port changes**: Update port checking logic
- **Database changes**: Update Docker container or RDS configuration
- **Changes to `.env` or `.env.template` files**: Ensure start-dev.sh populates correctly

### start-dev.sh Features

The script provides:
- Configurable local/remote service environments (--backend, --orchestrator, --agent-creator)
- Automatic Docker database management for local backend
- AWS SSM secret fetching for credentials
- Port conflict detection and resolution
- Process management with cleanup on interrupt
- Separate log files for each component
- Hot reload support for backend services

### Usage Examples

```bash
# Run everything locally
./start-dev.sh

# Run frontend locally, backend on dev
./start-dev.sh --backend dev

# Run frontend locally, all backends on staging
./start-dev.sh --backend stg --orchestrator stg --agent-creator stg

# Follow logs
tail -f logs/backend.log
tail -f logs/orchestrator.log
```


## Distributed Tracing with LangSmith

The system implements distributed tracing across the orchestrator and all A2A sub-agents (agent-creator, alloy-agent, ...) using LangSmith.

### How It Works

Tracing spans two mechanisms: **in-process** context propagation via Python `contextvars`, and **cross-process** propagation via HTTP headers.

1. **Orchestrator dispatch** (`app/orchestrator-agent/app/middleware/dynamic_tool_dispatch.py`): The `@traceable` decorator on `astream_a2a_agent()` creates a LangSmith `RunTree` and stores it in a `contextvars.ContextVar`. This context automatically propagates through all `await` chains within the function.

2. **Header injection** (`app/agent-common/agent_common/a2a/client_runnable.py`): `A2AClientRunnable._inject_trace_headers()` is an httpx event hook that fires before every HTTP request. It calls `get_current_run_tree()` — which reads from the same `contextvars.ContextVar` set by `@traceable` — and injects `langsmith-trace` and `baggage` headers into the outgoing request.

3. **Sub-Agent Side**: Both agent-creator and alloy-agent use `TracingMiddleware` from `langsmith.middleware` to extract trace headers from incoming requests and re-create the trace context, making all sub-agent operations appear as child spans.

4. **Result**: All operations appear as a single hierarchical trace in LangSmith with the orchestrator as the parent run and sub-agent operations as child runs.

### Key Implementation Details

- **Dynamic Header Injection**: Use httpx event hooks (`event_hooks={"request": [handler]}`) to inject trace headers at request time, not client initialization time. The hook reads from `contextvars` so headers reflect the current active span.
- **`contextvars` propagation**: `@traceable` → sets `RunTree` in `ContextVar` → `await` chain preserves it → `get_current_run_tree()` reads it in the httpx hook. This is why headers are always fresh per-request.
- **`config` parameter on `astream()`/`ainvoke()`**: `BaseA2ARunnable`, `A2AClientRunnable`, and `LocalA2ARunnable` all accept an optional `config: Dict` parameter matching LangChain's `Runnable` interface. For remote agents, the config is accepted for interface consistency but tracing relies on `contextvars` + httpx hooks. For local agents, the config carries callbacks, metadata, and tags used for checkpoint isolation and cost tracking.
- **Middleware Registration**: `TracingMiddleware` must be registered in the FastAPI app for both agent-creator and alloy-agent
- **Environment Variables**: Ensure `LANGSMITH_API_KEY`, `LANGSMITH_TRACING`, `LANGSMITH_ENDPOINT`, and `LANGSMITH_PROJECT` are configured for all services
- **Infrastructure**: CloudFormation templates must include `LANGSMITH_API_KEY` from SSM and proper IAM permissions

### When Adding New A2A Agents

To ensure new A2A agents participate in distributed tracing:

1. Add `TracingMiddleware` to the FastAPI app
2. Configure LANGSMITH environment variables in `.env.template`
3. Add LANGSMITH_API_KEY to CloudFormation task definition (from SSM)
4. Add SSM permission to read LANGSMITH_API_KEY in the execution role
5. Update `start-dev.sh` to fetch and set LANGSMITH_API_KEY for the new service

## A2A Extensions Protocol

The orchestrator emits structured streaming events via three custom A2A extensions. This is a cross-cutting concern spanning the orchestrator (emitter), playground-backend (proxy/filter), and playground-frontend (renderer).

### Extension URIs

- `urn:nannos:a2a:activity-log:1.0` — Tool usage & delegation status events (timeline)
- `urn:nannos:a2a:work-plan:1.0` — Structured todo checklist progress
- `urn:nannos:a2a:intermediate-output:1.0` — Streaming draft content from sub-agents

### How a Client Should Handle Responses

Extension events arrive as standard A2A `status-update` or `artifact-update` events. Clients classify them by inspecting `Message.extensions` or `Artifact.extensions`:

**1. Activity Log** (`status-update` with `message.extensions` containing `activity-log:1.0`):
- Extract text from `message.parts[0].text`
- Optionally read `message.metadata.source` for sub-agent attribution
- Display as a timeline/activity entry — do NOT show as a chat message bubble
- These are transient; the backend does not persist them

**2. Work Plan** (`status-update` with `message.extensions` containing `work-plan:1.0`):
- Extract `message.parts[0].data.todos` (array of `TodoItem` objects)
- Each todo has `name`, `state` (submitted/working/completed/failed), optional `source`, `target`
- Merge incoming todos by replacing all items from the same `source` group
- Display in a sticky progress widget, not in timeline or message history
- These are transient; the backend does not persist them

**3. Intermediate Output** (`artifact-update` with `artifact.extensions` containing `intermediate-output:1.0`):
- Extract text from `artifact.parts[0].text` and agent name from `artifact.metadata.agent_name`
- Append chunks from the same `agent_name` together (streaming)
- Display as expandable "thinking" blocks — NOT as the final answer
- The artifact ID has a `-thought` suffix to separate it from the main response artifact
- These are transient and should not be confused with the main response artifact

**4. Main Response** (artifact-update WITHOUT `intermediate-output` extension):
- Streaming text chunks with `append=true` — accumulate into the final message bubble
- This is the orchestrator's actual response that gets persisted

**5. Completed Status Update**:
- `status.state === "completed"` — finalize the streamed message
- If a nested `status.message` contains displayable text (no extension tags), show it as the final response

### Extension Activation (opt-in)

Clients send `X-A2A-Extensions` header listing desired extension URIs (comma-separated). If absent, **all extensions are disabled** (per A2A spec — extensions are opt-in). Clients must explicitly request the extensions they want to receive.

## Common Tasks

### Adding a New Environment Variable

1. Add to appropriate `.env.template` file
2. Update `start-dev.sh` to populate the variable:
   - For secrets: Add AWS SSM fetch
   - For config: Add default value or logic
3. Update Ansible Playbook and CloudFormation stacks if needed for deployed environments: `infrastructure/roles/`
4. Document in service-specific copilot instructions

### Adding a New Service

1. Create service directory under `app/`
2. Add `.github/copilot-instructions.md` for the service
3. Add `.env.template` with configuration variables
4. Update `start-dev.sh` to start the service
5. Add Ansible Playbook and CloudFormation stacks for deployment
6. Document dependencies and integration points

### Changing Database Schema

1. Create migration script in `infrastructure/roles/basis/files/ddl/scripts/`
2. Test migration on local database
3. Update SQLAlchemy models
4. Update repositories if needed
5. Run tests to verify changes

## Important Notes

- Never bypass repository pattern for database writes
- All secrets come from AWS SSM Parameter Store
- PostgreSQL credentials differ by role (service vs docstore)
- Frontend SDK is auto-generated from backend OpenAPI spec
- Orchestrator configuration depends on backend environment
