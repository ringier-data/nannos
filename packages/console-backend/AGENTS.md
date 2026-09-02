# Console Backend Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Tech Stack

- FastAPI with async/await
- SQLAlchemy 2.0+ (async) with PostgreSQL
- Postgresql for sessions and conversations
- Pydantic v2 for data validation
- pytest with pytest-asyncio for testing

## Local Development Environment

**CRITICAL: Any changes that impact the local development environment MUST be reflected in `/start-dev.sh`**

This includes:
- New environment variables (add to SSM fetching or default values)
- New secrets/credentials (add AWS SSM parameter fetching)
- Configuration changes that affect local setup
- New service dependencies or startup requirements
- Changes to `.env` or `.env.template` files

The `start-dev.sh` script is the single source of truth for local environment setup. Always update it when making changes that affect how the application runs locally.

## Code Style

- Use async/await for all database and I/O operations
- Type hints are required for all function signatures
- Use dependency injection via FastAPI's `Depends()`
- Prefer explicit over implicit error handling

## Python Environment

This project uses `uv` for dependency management:

```bash
# Install dependencies
uv sync

# Run Python commands
uv run python script.py

# Run tests (prefer runTests MCP tool when available)
uv run pytest tests/ -v

# Run with coverage
uv run pytest tests/ --cov=console_backend --cov-report=html
```

## File Writing Safety

NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits with proper file writing tools instead.

## Architecture Patterns

### Repository Pattern for Data Operations

**CRITICAL: All database write operations (INSERT/UPDATE/DELETE) MUST use the repository pattern to ensure automatic audit logging.**

#### How to Add New Data Operations

1. **Extend or create a repository** in `console_backend/repositories/`:
   - Inherit from `AuditedRepository` base class
   - Specify the entity type in the constructor
   - Override `create()`, `update()`, or `delete()` if custom logic is needed
   - Use base class methods for standard CRUD with automatic auditing

2. **Service layer integration**:
   - Services should use repositories for all data mutations
   - Pass the `actor: User` (user object containing the subject identifier) to repository methods
   - Repositories handle audit logging automatically

3. **Audit logging is automatic** when using repositories:
   - `create()` logs the full entity state after creation
   - `update()` logs before/after state changes (use `fetch_before=True`)
   - `delete()` logs the final entity state before deletion
   - Custom operations (approve, reject, etc.) call `audit_service.log_action()` directly

#### Example: Creating a New Repository

```python
from console_backend.repositories.base import AuditedRepository
from console_backend.models.audit import AuditEntityType

class MyEntityRepository(AuditedRepository):
    def __init__(self):
        super().__init__(
            table_name="my_entities",
            entity_type=AuditEntityType.MY_ENTITY,
            id_column="id"
        )
```

#### Example: Using Repository in Service

```python
from console_backend.repositories.my_entity_repository import MyEntityRepository

class MyEntityService:
    def __init__(self):
        self.repo = MyEntityRepository()
    
    async def create_entity(self, db: AsyncSession, actor: User, data: dict):
        entity_id = await self.repo.create(
            db=db,
            actor=actor,
            data=data
        )
        return entity_id
```

### DO NOT Create Direct SQL for Write Operations

❌ **WRONG** - Direct SQL write without audit:
```python
result = await db.execute(
    text("INSERT INTO my_table (name) VALUES (:name)"),
    {"name": name}
)
```

✅ **CORRECT** - Use repository:
```python
entity_id = await self.repo.create(
    db=db,
    actor_sub=user_id,
    data={"name": name}
)
```

## Audit Logging

### Audit Entity Types
Available in `AuditEntityType` enum:
- `USER` - User account operations
- `GROUP` - User group management
- `SUB_AGENT` - Sub-agent lifecycle
- `SESSION` - Session events (e.g., admin mode)
- `SECRET` - Secrets management

### Audit Actions
Available in `AuditAction` enum:
- `CREATE`, `UPDATE`, `DELETE` - Basic CRUD
- `APPROVE`, `REJECT` - Approval workflows
- `ASSIGN`, `UNASSIGN` - Resource assignments
- `SUBMIT_FOR_APPROVAL` - Workflow transitions
- `ACTIVATE`, `DEACTIVATE` - Entity state changes
- `SET_DEFAULT`, `REVERT` - Version management
- `PERMISSION_UPDATE` - Permission changes
- `ADMIN_MODE_ACTIVATED` - Security events

### Adding New Audit Types

1. Add enum value to `console_backend/models/audit.py`
2. Create database migration in `infrastructure/roles/basis/files/ddl/scripts/`
3. Use `ALTER TYPE` to add enum value (PostgreSQL doesn't support removing enum values)

## A2A Extension Event Processing

The agent-console proxies A2A events from the orchestrator to the frontend via Socket.IO. It classifies events by their extension markers and applies filtering logic.

### Event Filtering in `_process_a2a_response()` (app.py)

- **Work-plan events** (`message.extensions` contains `work-plan:1.0`): Forwarded to frontend via Socket.IO but NOT persisted to the database
- **Activity-log events** (`message.extensions` contains `activity-log:1.0`): Forwarded to frontend, persisted for history reconstruction
- **Intermediate-output artifacts** (`artifact.extensions` contains `intermediate-output:1.0`): Forwarded to frontend but NOT accumulated into `_streaming_buffers` (the main response buffer)
- **Main response artifacts** (no intermediate-output extension, `append=true`): Accumulated into `_streaming_buffers` for final message assembly
- **Terminal status-only events** (completed/failed with no message content): Skipped for persistence

### Persistence Rules

Only save to database when ALL of these are true:
- Not a work-plan event
- Not an artifact append (streaming chunk)
- Not a terminal-status-only signal

Activity-log events ARE persisted so the frontend can reconstruct timelines from `raw_payload` when loading message history.

## Database Migrations

- Migrations use Rambler and are located in `infrastructure/roles/basis/files/ddl/scripts/`
- Name format: `###_description.sql` (e.g., `016_add_secret_to_audit_enums.sql`)
- Migrations run automatically in test containers
- Always include `-- rambler up` and `-- rambler down` comments

## Testing

**Prefer the runTests MCP tool over terminal commands when running tests.**

Fallback to direct pytest commands when needed:
```bash
uv run pytest tests/ -v
uv run pytest tests/test_specific.py::test_function -v
```

### Test Structure
- Use `pg_session` fixture for database access (not `db_session`)
- **Prefer actual database verification over mocking** for audit logging tests
- Use `Mock()` for synchronous mocks, `AsyncMock()` for async operations
- When mocking `db.execute()`, use `AsyncMock()` but mock the result with `Mock()`

### Testing Audit Logging

**Best Practice: Verify actual database writes**
```python
@pytest.mark.asyncio
async def test_operation_logs_audit(pg_session):
    repo = UserRepository()
    
    # Perform operation
    await repo.create(
        db=pg_session,
        actor_sub="test-user-sub",
        fields={"id": "test-id", "name": "Test"},
        returning="id"
    )
    await pg_session.commit()
    
    # Verify audit log was written to database
    result = await pg_session.execute(
        text("SELECT * FROM audit_logs WHERE entity_type = 'user' AND entity_id = 'test-id' ORDER BY created_at DESC LIMIT 1")
    )
    audit_log = result.mappings().first()
    
    assert audit_log is not None
    assert audit_log["actor_sub"] == "test-user-sub"
    assert audit_log["action"] == "create"
    assert "after" in audit_log["changes"]
```

**Alternative: Mock only for complex operations**
```python
from unittest.mock import patch, AsyncMock

@pytest.mark.asyncio
async def test_complex_operation_logs_audit(pg_session):
    with patch('console_backend.repositories.sub_agent_repository.audit_service.log_action', new_callable=AsyncMock) as mock_audit:
        # Perform operation that has complex DB interactions
        await repo.approve_version(pg_session, context)
        
        # Verify audit was logged
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs['entity_type'] == AuditEntityType.SUB_AGENT
        assert call_kwargs['action'] == AuditAction.APPROVE
```

### DateTime Serialization
When storing datetime objects in JSON audit logs, use the `_serialize_for_audit()` helper in repositories to convert datetime objects to ISO format strings. The base repository's `update()` method automatically handles this serialization.

## Common Patterns

### Async Database Sessions
```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

async def my_function(db: AsyncSession):
    result = await db.execute(text("SELECT * FROM table"))
    return result.mappings().all()
```

### Service Singletons
Services use singleton pattern via `service_instances.py`:
```python
from console_backend.service_instances import sub_agent_service

# Use in routes/controllers
agent = await sub_agent_service.create_sub_agent(...)
```

### Notification Audiences Beyond Group Membership
Two things to get right when an audience is chosen by standing rather than by group
membership (`BugReportService._notify_administrators` is the worked example):

- **Exclude machine accounts explicitly; do not infer that a filter already excludes
  them.** Migration 041 seeds a `system` user with `role = 'admin'` — it owns
  auto-provisioned agents — and leaves `is_administrator` FALSE. That makes it tempting
  to conclude an `is_administrator` audience cannot reach it. It can: deployments promote
  that account (nothing in the code does, beyond `FIRST_USER_IS_ADMIN` for the very first
  user), and one was collecting bug-report notifications in an inbox nobody opens. The
  seed's column values are a starting state, not an invariant — reason from the rows a
  live database actually holds. There is no structural marker for machine accounts yet,
  so exclusion is by id.
- **Match the audience to read visibility, not to a write capability.** `bug_reports`
  grants `triage` to `approver`, but no read path honours it, so notifying triagers would
  hand out content the recipient is denied everywhere else. It is also the less durable
  key: a capability can be redefined or removed, while `is_administrator` is structural.

### Authorization Checks
```python
from console_backend.authorization import check_capability, check_action_allowed

# Check system-level capability
if check_capability(user.role, 'sub_agents', 'approve'):
    # User's system role allows approving sub-agents
    pass

# Check group role capability
if check_action_allowed(group_role, 'sub_agents', 'write'):
    # User's group role allows write actions
    pass
```

## Two-Layer RBAC (Role-Based Access Control)

The system implements a two-layer RBAC model that combines **system roles** with **group roles** to determine effective permissions.

### Layer 1: System Roles

System roles define **what actions a user can perform system-wide**. Defined in `SYSTEM_ROLE_CAPABILITIES`:

- **`member`**: Basic user with read/write access to resources in their groups
  - Can view groups they're in
  - Can manage members in their groups (requires group manager role)
  - Can read/write sub-agents (requires group access)
  - Can read/write secrets (requires group access)

- **`approver`**: Can approve submissions in accessible groups
  - All member capabilities
  - Can approve sub-agents (requires admin-mode + group write/manager role)

- **`admin`**: System administrator with elevated privileges
  - All approver capabilities
  - Admin-mode actions (`.admin` suffix) bypass group restrictions:
    - `read.admin`, `write.admin` - Access all resources system-wide
    - `approve.admin` - Approve any submission system-wide
  - Can manage users system-wide
  - All `.admin` actions require admin-mode to be enabled

### Layer 2: Group Roles

Group roles define **what actions a user can perform on resources within a specific group**. Defined in `GROUP_ROLE_CAPABILITIES`:

- **`read`**: Read-only access
  - View sub-agents, secrets, and members

- **`write`**: Can modify resources
  - Read/write sub-agents
  - View members
  - Read secrets

- **`manager`**: Full group management
  - Read/write sub-agents and secrets
  - Add/remove group members
  - Change member roles

### Permission Intersection Model

**Effective permissions = Resource permissions ∩ System role ∩ Group role**

#### How Permissions are Checked:

1. **System-level check** (`check_user_permission()`):
   - Verifies user's system role has the capability
   - Used for: viewing groups, system-wide operations
   - Does NOT check specific resource access

2. **Resource-level check** (`check_resource_permission()`):
   - Combines THREE factors:
     - System role capabilities (required for special actions like `approve`)
     - Resource permissions (what actions the group has on the resource)
     - Group role (what actions the user's role allows)
   - Special cases:
     - Owners always have full access
     - Public resources allow read access to all
     - `approve` action requires: approver/admin system role + write/manager group role

#### Example Scenarios:

```python
# Scenario 1: Member with 'read' group role
# - System role: member (allows read/write)
# - Group role: read (allows read only)
# - Resource permissions: ['read', 'write']
# → Effective: read only (limited by group role)

# Scenario 2: Approver with 'write' group role
# - System role: approver (allows read/write/approve)
# - Group role: write (allows read/write)
# - Resource permissions: ['read', 'write']
# - Action: approve
# → Effective: Can approve (has system approve + group write access)

# Scenario 3: Member with 'manager' group role
# - System role: member (no approve capability)
# - Group role: manager (allows read/write)
# - Resource permissions: ['read', 'write']
# - Action: approve
# → Effective: CANNOT approve (lacks system approve capability)
```

### Admin Mode

Actions with `.admin` suffix require admin-mode to be enabled:
- Admin-mode is a session-level toggle
- Provides audit trail for elevated operations
- Bypasses group permission intersection
- Only available to users with `admin` system role

### Authorization Helpers

```python
from console_backend.authorization import check_capability, check_action_allowed
from console_backend.services.user_group_service import user_group_service

# Check system role capability
can_approve = check_capability(user.role, 'sub_agents', 'approve')

# Check group role capability
can_write = check_action_allowed(group_role, 'sub_agents', 'write')

# Check full resource permission (combines all layers)
has_access = await user_group_service.check_resource_permission(
    db=db,
    user_id=user.id,
    resource_type='sub_agents',
    resource_id=sub_agent_id,
    action='write'
)
```

### When to Use Each Check

- **`check_capability()`**: Check if system role has a capability (e.g., can user approve?)
- **`check_action_allowed()`**: Check if group role allows an action
- **`check_user_permission()`**: Check system-level permissions (groups, users)
- **`check_resource_permission()`**: Check access to specific resources (sub-agents, secrets)

## Critical Design Decisions

### Steering Message Consumption Pattern (app.py)

When sending a steering message via `_send_steering_message_to_agent()`, the code uses `break` after the first event from `a2a_client.send_message()` — NOT `pass` to drain. The agent-console shares the same A2A SDK `Client` instance between the primary stream (`_send_message_to_agent`) and steering. The A2A SDK's `EventQueue.tap()` creates a child queue that receives all parent events. If leaked parent events containing raw `Task` objects were consumed through the shared `Client`, its `ClientTaskManager` would raise "Task is already set" errors. The `break` takes only the ack event and lets SSE teardown close the child queue. Note: consuming from the child never removes events from the parent queue (they're independent `asyncio.Queue` instances). See the root copilot instructions "Continuous Interaction Turns" section for the full mechanism.

### One Alias = One Deployment (admin_model_gateway_router)

`register_model` 409s when the alias is already registered on the gateway (checked BEFORE the
rate-card write, so a refused registration leaves nothing behind; fails open when the gateway can't
be listed). LiteLLM itself allows many deployments under one `model_name` and load-balances across
them — this console cannot express that: the rate card, the provider check and the role defaults are
all keyed on the alias, edit/delete address a single gateway id, and `edit_model` already reports a
surviving second deployment as a fault (`updated_with_stale_duplicate`). A duplicate alias silently
doubles routing for a model the admin can only manage half of. If replica/failover routing is ever
wanted, it needs an explicit flow, not a re-registration. The guard is registration-only: an edit
re-registers its own alias by design. `ModelGatewayPage` mirrors it (`aliasTaken`) — picking the
same catalog entry twice auto-fills the same alias, which is how duplicates happened.

### Rate-Card Provider Must Equal the litellm Provider Family (cost tracking ↔ rate cards)

Rate cards key on `(provider, model_name=alias)`. Billing resolves provider in the cost logger
(`litellm-proxy/custom_logger.py` `_build_record`): `custom_llm_provider`, else the gateway
model-id prefix (`deployment_id.split("/")[0]`). The rate-card provider MUST equal that family
(e.g. `vertex_ai`, `bedrock`) or usage never matches the rate card → the model silently bills
**$0**. Usage logging only *reads* rate cards (`calculate_cost`) — it never creates them; only
explicit register/edit (`admin_model_gateway_router`) and the Rate Cards page do.

ONE value carries all of it: the provider route on the deployment. It is what LiteLLM routes on,
what the cost logger stamps on usage, what provider-specific rules branch on
(`_with_default_vertex_location`, which credential params a deployment takes, the embedding request
profile) and what the rate card is keyed on — so those can never disagree. Registration requests
carry **no** provider field; `ModelRegistrationRequest` deliberately has none.

Enforcement lives entirely in `services/rate_card_service.py`, on BOTH write paths:
- register/edit resolve the route server-side (`_resolve_billing_provider`): the model id's prefix
  or `custom_llm_provider` (`runtime_billing_provider` — the cost logger's own rule), else an exact
  lookup of that id in the server's own catalog → `route_family(entry tag)` → prefixed onto the
  model id. The catalog path is the common one, not a fallback: LiteLLM's cost map keys Bedrock
  models by bare id (`eu.amazon.nova-2-lite-v1:0`). Nothing resolves → 422 asking for a prefixed
  id, EXCEPT when the catalog itself is unreadable (`get_catalog` degrades to a stale cache, then to
  `[]`): that is a 502, because reporting an outage as "not a known model" sends the admin off to
  debug an id that was never wrong. Never guess, since the route decides what bills. `GET /catalog`
  annotates every entry with its resolved `family`, so the picker displays the route (`bedrock`)
  instead of the cost-map tag (`bedrock_converse`) and no client re-implements the normalization.
- the Rate Cards page passes an admin-typed provider, so every service write path
  (`create_entry`, `create_model_rate_card`, `copy_model_rates`, `rekey_model_provider`) runs
  `assert_billable_provider` → 422 (`create_model_rate_card` uses `assert_routable_provider` instead
  when register/edit derived the value). Any NEW rate-card write must go through the service, never
  straight to the repository, or this invariant is bypassed and the card bills $0.

Which check applies depends on WHERE the provider came from. The two share no logic, so they are two
functions — do not fold them back into one with a mode flag:
- admin-typed → `assert_billable_provider`: the `runtime_provider_families()` allowlist (verified
  built-ins plus whatever `LLM_GATEWAY_PROVIDERS` adds, so integrating `mistral` needs no code change
  — for those tag == family, which is how litellm routes `mistral/…`). A typo and an un-integrated
  vendor are indistinguishable here, so the allowlist is the only guard available.
- derived from the deployment (register/edit only) → `assert_routable_provider`: only the TAG
  vocabulary is refused (`is_catalog_tag_vocabulary`). The value is the deployment's own route, which
  is by construction what `get_llm_provider` routes on and what the cost logger stamps, so any
  routable vendor is billable — applying the allowlist here 422'd `anthropic/…`, `groq/…`,
  `deepseek/…` with "not a runtime billing provider" immediately after the sibling error asked for
  that very prefix. An unroutable prefix is caught by the mandatory post-registration test call,
  which rolls it back.

Do NOT key or "correct" a card from the gateway's
`model_info.litellm_provider`: that is LiteLLM's cost-map *implementation tag*
(`bedrock_converse`, `vertex_ai-language-models`, `vertex_ai-anthropic_models`) — a different
vocabulary from the runtime family (`bedrock`, `vertex_ai`) the logger emits at call time
(`get_llm_provider` normalizes tags to families; verified on litellm 1.90.0). A card keyed on the
tag matches no usage → silent $0 billing; `assert_billable_provider` rejects it now, and
`route_family` is the ONLY sanctioned tag→family conversion. Reading the tag is legitimate in
exactly three places, none of which decide a billing key: catalog filtering against
`integrated_providers` (those are tags), the provider shown in the admin/app model lists when
nothing is derivable, and `cost-prefill`'s second lookup candidate so cards written before this
derivation existed still prefill their stored rates. Anywhere else, a tag compared against family
names is a bug — it silently takes the "unknown provider" branch. Also: `bedrock_converse/` is NOT
a routable model-id prefix in litellm 1.90.0 — never pin catalog tags as `custom_llm_provider`.

Safety net: `GET /api/v1/admin/rate-cards/provider-config` →
`services/provider_config_check.py`, rendered as the banner on the Rate Cards and Model Gateway pages
plus the `billing_rate_cards` System Status row. Configuration only, in both directions: every gateway
deployment's derived runtime provider must have an active card pricing its alias
(`unbillable_deployments`, so a mis-keyed model is caught before its first call), and no active card
may be keyed outside `runtime_provider_families()` (`orphan_cards` — the dead pricing migration 076
cleaned up by hand, findable with no traffic and no gateway). Deterministic and cheap: the gateway
list is already cached in `model_gateway_service`, the rest is two point queries. **No result cache
and no `days`** — the answer must be right the instant a fix lands, and the frontend only needs to
invalidate `PROVIDER_CONFIG_QUERY_KEY`. A healthy system returns two empty lists.

Deliberately NOT here: "what already billed $0" over a past window. Cost is computed at ingest, so
those rows cannot be retroactively priced, and most of them aren't even fixable — usage_logs is
written by two pipelines and the in-app SDK callback
(`ringier-a2a-sdk/cost_tracking/callback.py` `_detect_provider`) infers the provider from response
metadata, so post-ADR-0001 (every client is ChatOpenAI) it labels gateway calls **`openai`** and logs
a response's model id rather than the alias. Reporting that as a rate-card fault gives a check that
can never reach zero, and a re-key suggestion keyed on a provider the cost logger never emits for
that model would bill it $0. Root fix (open): SDK-based agents should attribute via the proxy
(spend_logs_metadata) instead of client-side detection. If a historical view is ever wanted, it
belongs in usage reporting, not in this check.

The check and billing share ONE SQL definition of "a card prices this model name" — `_model_match` /
`_entry_in_force` in `rate_card_repository.py`, used by `get_active_rate`, `get_all_active_rates` and
`find_card_providers_for_models`. Never hand-roll that predicate again: matching exact names only
hides pattern cards (how model families are priced here), and reading "active" as
`effective_until IS NULL` gets scheduled price changes backwards — a closed-ended entry with a future
`effective_until` is what bills today.

`POST /rekey` is the one-click fix: it moves a flagged card (pricing history included) to the runtime
key; 409 when the target key already has a card — including when a concurrent write claims it first
(uq_rate_card violation is translated inside a savepoint, never a 500).

A re-key is only ever offered for `rekey_candidates`, never for every card the alias has: a card that
prices another deployment of the same alias, or that matches only through a pattern on another model
name, is reported but not movable — re-keying it would un-bill traffic it correctly prices, or drag a
whole family's pricing along (add a card under the flagged provider instead).

Historical footgun: a Vertex **location** (`vertex_location` `eu`/`global`) is not a provider. The
registration form's Provider field was free-text, a location was typed there, and orphan
`eu`/`global` rate cards were created. That field no longer exists: `ModelGatewayPage.tsx` shows the
route READ-ONLY (`effectiveProvider` — model-id prefix, else the catalog entry's `family`), mirroring
the server's resolution instead of competing with it, and sends nothing. Register/edit return the
provider they actually keyed (`ModelRegistrationResponse.provider`) so the UI never badges a model
with a key that doesn't bill. Do not reintroduce an editable provider input: an admin-typed value in
the keying path is the whole class of bug this removes.

Related but NOT a keying problem — Bedrock availability is per-REGION, and AWS rejects a model that
isn't offered in the caller's region with "The provided model identifier is invalid": the same message
a genuinely wrong model id gets. Registration then rolls the deployment back, so it reads as "this
model can't be registered" (verified 2026-08-05: `amazon.nova-2-multimodal-embeddings-v1:0` exists in
us-east-1 only — sync `/v1/embeddings` works there — while `amazon.titan-embed-image-v1` is also in
eu-central-1). Nothing pins a default region for Bedrock (unlike `_with_default_vertex_location`): a
blank `aws_region_name` means the proxy pod's own region, surfaced to the UI as
`GatewayUiConfig.default_bedrock_region` (`AWS_BEDROCK_REGION`, else `AWS_REGION`). The registration
dialog states availability up front from `GET /bedrock-regions` →
`services/bedrock_availability_service.py` (ListFoundationModels + ListInferenceProfiles per probed
region, long-cached), and turns that AWS message into a region verdict on failure. That service is
ADVISORY: it needs `bedrock:ListFoundationModels`/`ListInferenceProfiles` (granted to the console pod
role in rcplus-alloy-infrastructure-agents `cf-iam-roles.yml`, catalog reads only — never `Invoke*`,
which stays with the gateway per ADR-0001), and when the probe can't run it returns `regions: null`
so the UI says nothing. `null` (can't tell) must never collapse into `[]` (AWS doesn't have it) —
that would accuse a working model id.

### Repository Pattern with Automatic Audit Logging (repositories/base.py)

ALL database write operations (INSERT/UPDATE/DELETE) MUST use the repository pattern. The `AuditedRepository` base class automatically logs every mutation with before/after state. Direct SQL writes bypass the audit trail. Repositories call `audit_service.log_action()` automatically in `create()`, `update()`, and `delete()` methods.

### Two-Layer RBAC with Permission Intersection (authorization.py, services/user_group_service.py)

Effective permissions = System Role ∩ Group Role ∩ Resource Permissions. System roles (`member`, `approver`, `admin`) define what users CAN do system-wide. Group roles (`read`, `write`, `manager`) define what their role ALLOWS in a group. Resource permissions define what a group HAS on a resource. Special actions like `approve` require BOTH system approver role AND group write access. Admin `.admin` suffix actions bypass group intersection but require admin-mode enabled.

### Real Database Testing Over Mocking (tests/)

Prefer actual PostgreSQL database writes for testing audit logging and data mutations, not mocks. This catches serialization issues, constraint violations, and race conditions that mocks would miss. Use `pg_session` fixture for real database operations.

### Skills Registry as Source of Truth (services/skill_registry_service.py, routers/playbook_router.py)

**Mental model**: Registry = PyPI (catalog), Docstore = .venv (runtime cache), Activation = pip install (pins a version), Self-update = author editing their own installed package.

The `skill_registry` PostgreSQL table is the single source of truth for all skill content. The docstore is a runtime cache only. Key tables:
- `skill_registry` — all skill content (authored or imported), with `owner_id`, `visibility` (private/group/public), `content_hash`, `group_ids[]`
- `skill_activations` — tracks which skills are active where, pinned to a `content_hash`

**Content-hash pinning**: Every registry edit produces a new SHA-256 hash. Activations pin to a specific hash. Consumers see "update available" when their pinned hash differs from the registry's latest.

**Self-update rule**: When an agent edits a skill it owns via MCP tools, only that agent's own activation auto-updates. Other consumers' activations stay pinned.

**Locked activations**: Created by config version approval only. Users cannot remove them. Stored in the same `skill_activations` table with `locked=true`.

### MCP Tool Endpoints (routers/playbook_router.py)

The playbook router exposes MCP-callable endpoints for agent self-improvement. These are called by agents via the console-backend MCP server:

- `console_create_skill` — Creates in registry + auto-activates on calling agent
- `console_update_skill` — Updates registry (new hash) + self-updates own activation
- `console_remove_skill` — Deactivates from agent (registry entry preserved)
- `console_activate_skill` — Activates existing registry skill on calling agent
- `console_update_playbook` — Updates AGENTS.md content
- `console_write_skill_file` / `console_delete_skill_file` — Manage bundled skill files
- `console_search_skills` / `console_import_skill` — Search/import from external sources

**CRITICAL**: MCP tools never create locked activations. Locked activations are managed exclusively through the config version approval workflow.

**Skill name validation**: Lowercase alphanumeric + hyphens only, 1-64 chars, no leading/trailing hyphens, no consecutive hyphens. Follows agentskills.io specification.

**File path validation**: Relative paths only, no traversal (`..`), max 6 segments deep, cannot be `SKILL.md` (managed via skill create/update).

### Skill Activations (services/skill_activation_service.py)

The activation service manages the lifecycle:
- `activate()` — Pin skill to current hash, write snapshot to docstore
- `deactivate()` — Remove activation + docstore entry
- `update_activation()` — Pull latest hash from registry, refresh docstore
- `self_update()` — Auto-called when author edits own skill
- `upsert_locked()` — Called by config version approval workflow
- `list_for_agent()` — All activations for a sub-agent (with update-available status)

## Important Notes

- Never bypass the repository pattern for data mutations
- All write operations must generate audit logs
- Tests must verify audit logging behavior
- DateTime objects must be serialized before JSON encoding in audit logs
- The repository pattern provides automatic enforcement of audit requirements
