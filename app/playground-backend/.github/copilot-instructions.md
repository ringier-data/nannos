# Playground Backend Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Tech Stack

- FastAPI with async/await
- SQLAlchemy 2.0+ (async) with PostgreSQL
- DynamoDB for sessions and conversations
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
uv run pytest tests/ --cov=playground_backend --cov-report=html
```

## File Writing Safety

NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits with proper file writing tools instead.

## Architecture Patterns

### Repository Pattern for Data Operations

**CRITICAL: All database write operations (INSERT/UPDATE/DELETE) MUST use the repository pattern to ensure automatic audit logging.**

#### How to Add New Data Operations

1. **Extend or create a repository** in `playground_backend/repositories/`:
   - Inherit from `AuditedRepository` base class
   - Specify the entity type in the constructor
   - Override `create()`, `update()`, or `delete()` if custom logic is needed
   - Use base class methods for standard CRUD with automatic auditing

2. **Service layer integration**:
   - Services should use repositories for all data mutations
   - Pass the `actor_sub` (user ID) to repository methods
   - Repositories handle audit logging automatically

3. **Audit logging is automatic** when using repositories:
   - `create()` logs the full entity state after creation
   - `update()` logs before/after state changes (use `fetch_before=True`)
   - `delete()` logs the final entity state before deletion
   - Custom operations (approve, reject, etc.) call `audit_service.log_action()` directly

#### Example: Creating a New Repository

```python
from playground_backend.repositories.base import AuditedRepository
from playground_backend.models.audit import AuditEntityType

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
from playground_backend.repositories.my_entity_repository import MyEntityRepository

class MyEntityService:
    def __init__(self):
        self.repo = MyEntityRepository()
    
    async def create_entity(self, db: AsyncSession, user_id: str, data: dict):
        entity_id = await self.repo.create(
            db=db,
            actor_sub=user_id,
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

1. Add enum value to `playground_backend/models/audit.py`
2. Create database migration in `infrastructure/roles/basis/files/ddl/scripts/`
3. Use `ALTER TYPE` to add enum value (PostgreSQL doesn't support removing enum values)

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
    with patch('playground_backend.repositories.sub_agent_repository.audit_service.log_action', new_callable=AsyncMock) as mock_audit:
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
from playground_backend.service_instances import sub_agent_service

# Use in routes/controllers
agent = await sub_agent_service.create_sub_agent(...)
```

### Authorization Checks
```python
from playground_backend.authorization import check_capability, check_action_allowed

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
from playground_backend.authorization import check_capability, check_action_allowed
from playground_backend.services.user_group_service import user_group_service

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

### Repository Pattern with Automatic Audit Logging (repositories/base.py)

ALL database write operations (INSERT/UPDATE/DELETE) MUST use the repository pattern. The `AuditedRepository` base class automatically logs every mutation with before/after state. Direct SQL writes bypass the audit trail. Repositories call `audit_service.log_action()` automatically in `create()`, `update()`, and `delete()` methods.

### Two-Layer RBAC with Permission Intersection (authorization.py, services/user_group_service.py)

Effective permissions = System Role ∩ Group Role ∩ Resource Permissions. System roles (`member`, `approver`, `admin`) define what users CAN do system-wide. Group roles (`read`, `write`, `manager`) define what their role ALLOWS in a group. Resource permissions define what a group HAS on a resource. Special actions like `approve` require BOTH system approver role AND group write access. Admin `.admin` suffix actions bypass group intersection but require admin-mode enabled.

### Real Database Testing Over Mocking (tests/)

Prefer actual PostgreSQL database writes for testing audit logging and data mutations, not mocks. This catches serialization issues, constraint violations, and race conditions that mocks would miss. Use `pg_session` fixture for real database operations.


## Important Notes

- Never bypass the repository pattern for data mutations
- All write operations must generate audit logs
- Tests must verify audit logging behavior
- DateTime objects must be serialized before JSON encoding in audit logs
- The repository pattern provides automatic enforcement of audit requirements
