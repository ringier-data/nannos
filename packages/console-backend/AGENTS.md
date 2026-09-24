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

# Run tests in parallel (all workers share one Postgres container, each test still gets its own cloned database)
uv run pytest tests/ -n auto

# Run with coverage
uv run pytest tests/ --cov=console_backend --cov-report=html
```

## File Writing Safety

NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits with proper file writing tools instead.

## Architecture Patterns

### Repository Pattern for Data Operations

**CRITICAL: All database write operations (INSERT/UPDATE/DELETE) MUST use the repository pattern to ensure automatic audit logging.**

The one deliberate exemption is cache bookkeeping that carries no business meaning: `users.entitlements_touched_at` (`services/entitlement_version.py`), bumped so the orchestrator's per-user entitlement version moves for gateway-held state. The admin action that triggers it is audited on its own.

Sign-in state is the other: `sessions`, `user_offline_tokens`, and the token broker's `broker_login_requests` and `broker_client_users` (`repositories/broker_login_request_repository.py`). They are written directly because they record a sign-in, whose business effect (the user upsert) is audited where it happens. The broker's client registry (`broker_clients`) is admin data and goes through `BrokerClientRepository` like any other.

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

- Migrations use Rambler and are located in `sqlmigrations/ddl/`
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

- **Filter out machine accounts with `AND is_service_account IS FALSE`.** Two rows exist
  in every deployment with no person behind them: the seeded `system` owner of
  auto-provisioned agents (migration 041) and a `service-account-<client>` row that
  console-backend onboards the first time a service calls it with a client-credentials
  token. Both have been found flagged `is_administrator` in a live database, so an
  audience keyed on that flag reaches them and fills inboxes nobody opens.

  The flag (migration 088) is set at creation from the token's own claims — see
  `dependencies.token_is_service_account`. Use it rather than an id list: an id list is
  one instance of the category, and the previous version of this note argued the seed's
  `is_administrator = FALSE` default made even that redundant. It did not. A seed's column
  values are a starting state, not an invariant; reason from the rows a live database holds.
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

### A SCIM-Provisioned User Has No IdP Identity Until First Login (models/user.py, scim_service, user_service)

`ScimUserService.create_user` writes `placeholder_sub(id)` — `scim-pending:<id>` — into
`users.sub`, because provisioning creates nobody in Keycloak; the real subject arrives with the
first OIDC login, through the email-matched upsert in `UserService._upsert_user_internal` (matched
on `LOWER(email)`, since SCIM stores the address verbatim and that path lowercases). Anything
handing `users.sub` to an IdP must ask `has_idp_identity(sub)` first;
`UserGroupService._mirror_membership_to_keycloak` is the single enforcement site, and it defers
rather than raising `404 User not found`.

**The placeholder is explicit because inference could not be made sound.** It used to be the row's
own id, recognised by `sub == id` narrowed by `scim_user_name IS NOT NULL`. Both halves fail:
migration 001 keyed `users` by the OIDC sub itself, so rows from that era legitimately have
`sub == id`, and `scim_user_name` can be written onto *any* row by a SCIM PUT/PATCH — including one
of those, which silently stopped mirroring a user who did have a Keycloak account. Migration 101
rewrote the existing placeholders; that migration is the only place the old inference is applied,
once, to the data as it stood.

**`users.keycloak_mirror_pending` is what makes the push retryable.** A deferred *add* raises it;
a successful reconciliation clears it. The trigger is that flag, not the placeholder-to-real
subject transition — that transition happens exactly once, so a Keycloak outage during that one
login used to leave the two sides diverged forever. Reconciliation pushes the *current* membership
set rather than a recorded backlog, which is what makes the retry safe: the calls are idempotent,
and a membership granted and withdrawn while the user was pending needs no replay. It stays inside
the login transaction deliberately — a rollback after a successful push simply leaves the flag set
and re-pushes next time, which is cheaper than restructuring the auth path around a post-commit
hook.

The database is the authority for membership (`/api/v1/auth/me` reads it from there); Keycloak
backs only the groups claim other OIDC clients consume. So the mirror never fails an operation: a
Keycloak error during reconciliation is logged, the flag stays set, and the login proceeds.

One consequence worth knowing: a placeholder subject resolves to nobody, so
`spend_attribution.resolve_user_sub` returns `None` for one and `billing_subject` falls back to the
internal id, which the usage ingest does resolve.

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

#### Voice-agent rate cards are seeded — the one exception to "no seeding"

The voice agent calls Vertex AI directly, so its models are keyed on the real family
`vertex_ai` (no provider exception needed). But **nothing else would ever create their
cards**: migration 076 stopped seeding on the premise that every model gets its card at
registration, and these two never touch the gateway, so no registration runs for them.
Without a card `calculate_cost` fails closed to `Decimal("0.00")`, so every voice call
would silently bill nothing.

That is why **migration 093 seeds them** — a deliberate, documented exception to 076, not
an oversight. Anything else billed outside the gateway needs the same treatment.

Prices in USD per **million** tokens (Vertex Standard tier, verified 2026-08-19 at
cloud.google.com/vertex-ai/generative-ai/pricing — the page lists the Live model as
"Gemini 2.5 Flash Live API", never "native audio"):

| provider | model_name | billing unit | flow | $/1M |
|---|---|---|---|---|
| `vertex_ai` | `gemini-live-2.5-flash-native-audio` | `audio_input_tokens` | input | 3.00 |
| | | `audio_output_tokens` | output | 12.00 |
| | | `base_input_tokens` | input | 0.50 |
| | | `base_output_tokens` | output | 2.00 |
| | | `tool_use_input_tokens` | input | 0.50 |
| `vertex_ai` | `gemini-2.5-flash` | `base_input_tokens` | input | 0.30 |
| (MCP tool risk scorer) | | `base_output_tokens` | output | 2.50 |
| | | `cache_read_input_tokens` | input | 0.03 |
| | | `tool_use_input_tokens` | input | 0.30 |

`model_name` must match what the agent reports — `GEMINI_MODEL_ID` and
`GEMINI_RISK_SCORER_MODEL` in `voice-agent/voice_agent/agent.py`.

**Live sessions re-bill the whole context every turn**, so voice call cost compounds with
call length. Google's Vertex pricing page states it in a footnote to the Live API tables —
"You are charged per turn for all tokens present in the Session Context Window… tokens from
past turns are re-processed and accounted for in each new turn" — and the Live API
best-practices pages repeat it under a heading called "Re-billing". Consequences:

- `fold_usage_into` **sums every unit**. Each `usageMetadata` report shows the context
  cumulatively, but the charge recurs per turn, so the sum is the billed total. An earlier
  version took the max on the input side and under-billed a measured 10-turn call by 2.6x.
- **`contextWindowCompression` is the only documented lever** against the compounding: after
  a compression the API "bills subsequent turns only for the retained history plus any new
  tokens". `build_live_config` currently sets `trigger_tokens=128000`, which never fires at
  realistic call lengths (a measured call peaked at 1,490) — so it provides no mitigation
  today. Lowering it trades conversational memory for cost and is a product decision.
- **Context caching does not apply.** The model page says "Context caching: Not supported",
  no Live SKU has a caching variant, and pricing shows N/A — so no cache discount softens
  the re-billed history.

**`tool_use_input_tokens`** is the voice agent's own unit for `tool_use_prompt_token_count`,
priced as ordinary input. Kept separate from `base_input_tokens` so it stays visible in the
breakdown and can be repriced on its own line.

**Cached input is discounted, never added on top.** `promptTokenCount` is cache-INCLUSIVE
per Google's docs, so emitting a cache unit alongside the full prompt count bills the
cached tokens twice. `_discount_cached_from_input` moves them out of the full-price units
first, matching the convention the gateway already established (`base_input = total_input -
cache_read - cache_creation`, `litellm-proxy/custom_logger.py`) after hitting the identical
bug on normalized Anthropic usage. On the Live path this is **defensive only** (caching is
unsupported there, per above) but it must stay: the risk scorer `gemini-2.5-flash` does
support caching and shares the same mapping.

Deliberately unpriced: **`cache_read_input_tokens` on the Live model** — no published rate
exists to enter. The unit is still reported if Gemini ever returns cached tokens, which
surfaces as a "missing rate card / partial cost" warning: the signal to go find the rate,
not a bug to silence.

Both models have retirement dates (Live: 2026-12-13, flash: 2026-10-20). Cards are
time-versioned, so a successor model needs its own card — usage on an unpriced model bills $0
silently, and only the Rate Cards banner will say so.

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
- `activate()` — Pin skill to current hash, write snapshot to docstore (personal/group) or
  add a REFERENCE to the sub-agent's config (sub-agent scope). Takes `mode` (ADR-0011):
  `pinned` (default) or `following`; re-activating with the other mode switches it.
- `deactivate()` — Remove activation + docstore entry / config reference
- `update_activation()` — Pull latest hash from registry, refresh docstore
- `self_update()` — Auto-called when author edits own skill
- `bump_following_referrers()` — Content-changed hook (registered in `service_instances.py`):
  every `following` referrer of the changed row gets one auto-approved version with the new
  hash, in the writer's transaction, one savepoint each; failures land in `last_bump_error`.
- `list_for_agent()` — All activations for a sub-agent (with update-available status, mode)

**References, not copies (ADR-0011)**: a sub-agent that activates a registry row it does not
own — another agent's public sub-agent skill or a standalone import — holds `{registry_id,
content_hash}` in its config and nothing else. `resolve_imported_skills` branches on OWNERSHIP
(`skill_registry.sub_agent_id == agent`), not on the row's scope: the owner sees always-latest,
a referrer is pinned by hash with `update_available`/`latest_hash` and `mode`/`bump_error` set.
A row other agents reference cannot be deleted or made private: `SkillRegistryService` raises
`SkillReferencedError`, which the routers map to 409 with the referrers listed.

### Shared Scheduled Jobs: Definitions and Subscriptions (ADR-0010)

The reasoning lives in `docs/adr/0010-shared-scheduled-jobs-run-once-per-subscriber.md`; this
section is the vocabulary and the contract the code implements (migrations 100/101,
`scheduled_job_repository.py`, `scheduler_service.py`, `scheduler_router.py`).

A scheduled job is two rows. The **definition** (`scheduled_job_definitions`) is what the job *is* —
name, kind, prompt/sub-agent or check tool and condition, `max_failures`, the **trigger defaults**
(schedule plus an optional timezone) and the **trigger policy** (`overridable | fixed`; watches
default to fixed). It is owned by one user and shared to groups with `read`/`write` through
`scheduled_job_definition_permissions`, exactly like a sub-agent (`read` = may subscribe and copy,
`write` = may edit, suspend, share on; delete stays with the owner or an admin). A definition never
runs. A **subscription** (`scheduled_job_subscriptions`) is one user's activation of it — `enabled`,
the trigger in force, delivery target, run bookkeeping, `last_check_result` — and every run
(`scheduled_job_runs.subscription_id`) is a subscription's run under that **subscriber's** identity:
their offline token, their bypass rules, their spend (`usage_logs.scheduled_job_id` is a
subscription id), their delivery DM, their auth asks (ADR-0009 is per subscription). N subscribers
means N runs; the owner is just another subscriber.

The **job** the API serves (`ScheduledJob`) is the *job view*: a subscription with its definition
folded in, keyed by the **subscription id** — what every client, link and notification has always
called the job id (migration 103 gives every pre-existing job one definition and one subscription
carrying the old id). The trigger fields carry the trigger *in force*; `trigger_inherited` says
whether that is the definition's defaults or the subscriber's own **override**, and
`trigger_defaults` carries the defaults for a writer. A definition's fields are live for every
subscription on its next run; **triggers propagate by inheritance** (an inherited subscription
follows later edits to the defaults) and `enabled` never propagates. A NULL default timezone means
*each subscriber's own* (`user_settings.timezone`), so `0 9 * * 1-5` is 09:00 local for everyone;
the job view resolves it (`COALESCE(subscription, definition, subscriber settings)`).

**One update path.** `PATCH /jobs/{id}` (`scheduler_update_job`) routes each field server-side:
definition fields need `write`; `enabled` and `delivery_channel_id` are always the caller's own; the
trigger takes an optional `scope: mine | everyone` that only matters once the definition has other
subscribers (alone, a writer's edit lands on the defaults so a later share inherits it). `scope` also
picks the baseline the **unchanged-echo filter** judges against (`_trigger_baseline`): the console
resends every trigger field prefilled, so an echo must not read as an edit — but it echoes the
*effective* trigger while `everyone` edits the *definition's default*, and for an editor who holds an
override those are different values. Judging both against the effective one made "promote my own
schedule to the default" a silent no-op and let an echo of the default rewrite it. Two carve-outs:
only a scope the caller actually SENT gets the definition baseline (one that merely resolved to
`everyone`, as it does for a sole subscriber, carries no intent to promote — otherwise a rename
rewrites the default), and `timezone` keeps the effective baseline (the job's is the resolved zone
and the form has no field for the definition's, so re-baselining would read every save as a request
to pin one). The everyone-merge also takes its KIND from the effective trigger, not the definition:
the form sends only the field matching the kind it displays and never `schedule_kind`, so reading
the kind off the definition silently dropped a sent cron whenever the default was an interval. Setting the
policy to `fixed` resets every override. **Suspend** (definition, `write`, holds every subscription
out of `claim_due_jobs`, preserves each `enabled`) is distinct from **pause/disable** (subscription,
mine). `DELETE /jobs/{id}` deletes the definition and every subscription when the caller owns it,
and merely unsubscribes otherwise. **Copy** (`read`) makes an independent definition; an inline
`automated` agent is copied with it, a referenced agent is referenced.

**Access is checked per subscriber at every dispatch** (`SchedulerEngine._agent_access_check`):
a subscriber who can no longer reach the definition's sub-agent has that one subscription paused
("Agent not accessible", no failure count, nobody else's touched). A definition may not be shared to
a group whose members cannot reach its regular sub-agent; sharing never grants agent access. The
one exception is an inline `automated` agent, which travels with the definition:
`SubAgentService.get_accessible_sub_agents` has a subscription arm for exactly that case.

**Group defaults** (`user_group_default_jobs`) mirror default agents: only a definition already
shared to the group qualifies; every current member is subscribed (enabled, inherited,
`activated_by = 'group'`) and every future member on join (`UserGroupService` calls
`SchedulerService.on_members_added/removed`). On leave, group-default subscriptions from that group
are removed; self-made ones on a grant the member no longer holds are disabled with "access
revoked" so re-adding restores their customisation. Durable console notifications cover what
changes what runs under *your* identity or what you own (`job_shared`, `job_access_revoked`,
`job_permission_changed`, `job_subscription_activated`, `job_subscription_reset`, `job_suspended`,
`job_resumed`, `job_deleted`); a writer editing a shared prompt is deliberately not one of them.

**Two things reach a subscriber outside the console.** A member the group default just
auto-subscribed gets an **activation notice** on their own delivery channel as well as the console
notification — `SchedulerService._dm_activations`, sent *after* the caller's commit (a DM saying a
job now runs under your identity must not arrive for a row a rollback removed) through
`SchedulerEngine.send_plain_notice`, a line posted under a subscriber's identity while running no
agent. It and `_publish_check_ask` (a watch's check-tool park, delivered as the authorization card
with the run id and the ask on `auth_ask`) are the two callers of `_dispatch_notice`, the one
agent-less dispatch seam. And every delivered run of a job whose subscriber is
not its owner carries a **provenance line** — `SchedulerEngine._provenance_line` puts it in the
dispatch metadata as `scheduled_job_provenance`, and agent-runner appends it to `agent_message`,
where every run's output is already composed. One seam, rather than the same footer in three
delivery clients; `None` for a job nobody else runs, which is most of them.

**What the model may do with all this** is the same set of routes with `tags=["MCP"]`:
`scheduler_list_shared_jobs`, `scheduler_subscribe_job` / `_unsubscribe_job`, `scheduler_copy_job`,
`scheduler_share_job`, `scheduler_suspend_job` / `_unsuspend_job`,
`scheduler_reset_job_schedules` / `scheduler_follow_default_schedule` (every subscriber, needing
write, versus the caller's own, needing nothing — inheritance is a door that opens both ways, and
a `null` trigger on `scheduler_update_job` is REFUSED rather than silently ignored, so "clear my
schedule" cannot look like it worked), `scheduler_add_group_default_job` / `_remove_group_default_job`,
plus one read tool on groups — `console_list_my_groups` (`GET /api/v1/groups/summaries`), which
returns id, name, description and **member_count only**: a share is decided on "which group, and
how many people does that mean", and the identities are not part of that question. `is_public` is
admin-only and not exposed. The vocabulary itself is a DB seed, so it ships as prompt migration
104 (targeted `replace()` calls in the style of 085, checked at the text level by
`tests/test_migration_104_task_scheduler_sharing.py` — an unmatched search string is a silent
no-op in SQL).

### Scheduled Run Vocabulary and Interruption (services/scheduler_engine.py)

The reasoning lives in `docs/adr/0007-interrupted-runs-get-one-fresh-attempt.md`; this section is
the vocabulary and the contract the code implements (migrations 091/092, `scheduler_engine.py`,
`scheduled_job_repository.py`, `utils/a2a_dispatch.py`).

A **job** here is a subscription (see the previous section) — a schedule plus a prompt, run under
its subscriber. A **run** is one recorded execution of it (`scheduled_job_runs`), created at dispatch and closed by
`_finalize`. Runs of the same job are independent **attempts**: nothing is carried between them, and
they never overlap — `claim_due_jobs` skips a job while any of its runs is still `running`.

Every run records a **trigger** (`RunTrigger`): `scheduled` for an ordinary occurrence, `retry` for
the one fresh attempt an interruption earns, `manual` for a user's run-now. The trigger is decided
where the run is created — by the claim (which returns it as `ClaimedJob.trigger`) or by the run-now
route — and the healer reads it off the row, so both paths that notice an interruption agree on
what it is worth.

An **interrupted run** (`JobRunStatus.INTERRUPTED`) is one that ended because the process executing
it died. It is *not* a failure: `complete_job` leaves `consecutive_failures` untouched, so process
churn can never trip `max_failures`. Only `dispatch_streaming` can say the agent died — it raises
`AgentUnreachable` for a transport error, a dropped or timed-out stream, or a 502/503/504, looking
through the a2a SDK's wrapping — and `_dispatch_job` classifies on that exception alone. A Keycloak
or database error on the way to the dispatch is a failure of the run.

What an interruption earns depends on the trigger:

- `scheduled` → the job gets `retry_at` (a **retry marker**, `RETRY_DELAY_SECONDS` out). It is a
  separate column from `next_run_at` so the schedule users read is never rewritten, and it is the
  claim loop's second wake-up reason. `complete_job` only ever *writes* it (COALESCE), because runs
  of one job can complete out of order; the claim consumes it, and pause/resume/PATCH-disable clear
  it. The retry branch ignores `enabled` (a retired `once` job is disabled too) and trusts
  `paused_reason IS NULL`, so every deliberate stop — pause, auto-pause, PATCH `enabled=false`,
  an unresolvable timezone — writes a reason.
- `retry` → recovery is exhausted. The run is marked as owing the user a notice (`notice_due_at`,
  written in the same statement as the run's outcome) and no further attempt is scheduled.
- `manual` → nothing. The user is present and can press again.

**Liveness.** A dead `agent-runner` is seen by the dispatcher through the stream (`read=300.0` is
the inter-event timeout). A dead `console-backend` is seen by the healer: the dispatcher heartbeats
`last_seen_at` every `HEARTBEAT_INTERVAL_SECONDS`, and `interrupt_stale_runs` sweeps on staleness
(`STALE_RUN_AFTER_SECONDS`), never on age, so a slow run is never shot and the healer stays correct
with several scheduler processes. A run with *no* heartbeat was written by the previous release and
may still be executing there during a rolling deploy; it keeps the old age bound
(`HEARTBEATLESS_RUN_AFTER_SECONDS`). `complete_run` only finalises a run still `running`, so a run the
healer already called interrupted stays interrupted if its dispatcher turns out to be alive; if
`complete_run` itself fails, `close_run_minimally` still closes the row, because a finished run left
`running` would be swept and re-executed.

**The user is told only when recovery is exhausted**, via `_notify_recovery_exhausted`: an
ephemeral notification-only A2A dispatch (text plus the job's push config, no `sub_agent_id`, no
run row) that agent-runner delivers while running no agent. Result delivery otherwise stays with
the executing agent — anything else the scheduler has to say goes through `_dispatch_notice`; do
not add another deliverer beside it. Notices are owed, not sent, where the loss is
detected; `_deliver_due_notices` claims them on a later tick, retries a failed delivery, and abandons
one that is older than `NOTICE_GIVE_UP_AFTER_SECONDS` or superseded by a later completed run.

## Important Notes

- Never bypass the repository pattern for data mutations
- All write operations must generate audit logs
- Tests must verify audit logging behavior
- DateTime objects must be serialized before JSON encoding in audit logs
- The repository pattern provides automatic enforcement of audit requirements
