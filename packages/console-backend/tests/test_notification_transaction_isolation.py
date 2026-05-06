"""Test that notification failures don't rollback critical data changes."""

import os

# Set up boto3 mock environment before any imports that use boto3
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from unittest.mock import AsyncMock, patch

import pytest
from aiomoto import mock_aws
from console_backend.models.secret import SecretCreate, SecretType
from console_backend.models.user import User
from console_backend.services.secrets_service import SecretsService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def _create_user(
    session: AsyncSession,
    email: str,
    sub: str,
    first_name: str = "Test",
    last_name: str = "User",
    is_admin: bool = False,
    role: str = "member",
) -> str:
    """Create a test user and return their ID."""
    query = text("""
        INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, created_at, updated_at)
        VALUES (:sub, :sub, :email, :first_name, :last_name, :is_admin, :role, NOW(), NOW())
        ON CONFLICT (sub) DO UPDATE SET email = :email, is_administrator = :is_admin, role = :role
        RETURNING id
    """)
    result = await session.execute(
        query,
        {
            "sub": sub,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "is_admin": is_admin,
            "role": role,
        },
    )
    user_id = result.scalar_one()
    await session.commit()
    return user_id


@mock_aws
@pytest.mark.asyncio
async def test_secret_permissions_persist_when_notifications_fail(
    pg_session: AsyncSession,
    secrets_service: SecretsService,
    test_user_db: User,
    test_approver_user_db: User,
):
    """Test that secret permission updates are committed even if notifications fail.

    This is a critical behavior: permission changes should be persisted to the database
    before notification processing, so that notification failures don't cause data loss.
    """
    # Create a secret
    secret = await secrets_service.create_secret(
        db=pg_session,
        actor=test_user_db,
        data=SecretCreate(
            name="test-secret",
            description="Test secret for transaction isolation",
            secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
            secret_value="secret-value-123",
        ),
    )
    assert secret is not None

    # Create a group to share with
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description) 
            VALUES (1, 'Test Group', 'Test group')
        """)
    )

    # Add a member to the group
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, :user_id, 'read')
        """),
        {"user_id": test_approver_user_db.id},
    )
    await pg_session.commit()

    # Mock notification service to raise an exception
    mock_notification_service = AsyncMock()
    mock_notification_service.bulk_create_notifications.side_effect = Exception("Notification service down!")
    secrets_service.set_notification_service(mock_notification_service)

    # Update permissions - this should succeed even though notifications fail
    result = await secrets_service.update_permissions(
        db=pg_session,
        secret_id=secret.id,
        group_permissions=[{"user_group_id": 1, "permissions": ["read"]}],
        actor=test_user_db,
        is_admin=False,
    )

    assert result is True

    # Verify permissions were actually written to database
    perms_result = await pg_session.execute(
        text("""
            SELECT user_group_id, permissions 
            FROM secret_permissions 
            WHERE secret_id = :secret_id
        """),
        {"secret_id": secret.id},
    )
    perms = perms_result.fetchall()

    assert len(perms) == 1
    assert perms[0][0] == 1  # user_group_id
    assert perms[0][1] == ["read"]  # permissions

    # Verify audit log was created
    audit_result = await pg_session.execute(
        text("""
            SELECT entity_type, entity_id, action 
            FROM audit_logs 
            WHERE entity_type = 'secret' AND entity_id = :secret_id 
            ORDER BY created_at DESC LIMIT 1
        """),
        {"secret_id": str(secret.id)},
    )
    audit = audit_result.fetchone()

    assert audit is not None
    assert audit[0] == "secret"
    assert audit[1] == str(secret.id)
    assert audit[2] == "permission_update"


@mock_aws
@pytest.mark.asyncio
async def test_secret_permissions_commit_before_notification_queries(
    pg_session: AsyncSession, secrets_service: SecretsService, test_user_db: User, test_approver_user_db: User
):
    """Test that permissions are committed before any notification queries run.

    If notification queries fail (e.g., invalid SQL), the permissions should already be saved.
    """
    # Create test user
    await _create_user(pg_session, "owner@test.com", "test-user-1")

    # Create a secret
    secret = await secrets_service.create_secret(
        db=pg_session,
        actor=test_user_db,
        data=SecretCreate(
            name="test-secret-2",
            description="Test secret",
            secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
            secret_value="secret-value-456",
        ),
    )
    assert secret is not None

    # Create a group
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description) 
            VALUES (2, 'Test Group 2', 'Test group 2')
        """)
    )
    # add a member to the group
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (2, :user_id, 'read')
        """),
        {"user_id": test_approver_user_db.id},
    )
    await pg_session.commit()

    # Mock the db.execute to fail on notification queries (queries with user_group_members)
    # but succeed on permission updates
    original_execute = pg_session.execute

    async def mock_execute(statement, *args, **kwargs):
        statement_str = str(statement)
        # Fail on member lookup queries that come AFTER permission updates
        if "user_group_members" in statement_str and "ANY(" in statement_str:
            raise Exception("Simulated member query failure!")
        return await original_execute(statement, *args, **kwargs)

    with patch.object(pg_session, "execute", side_effect=mock_execute):
        # This should succeed - permissions committed before the failing query
        result = await secrets_service.update_permissions(
            db=pg_session,
            secret_id=secret.id,
            group_permissions=[{"user_group_id": 2, "permissions": ["write"]}],
            actor=test_user_db,
            is_admin=False,
        )

        assert result is True

    # Verify permissions were saved despite notification query failure
    perms_result = await pg_session.execute(
        text("""
            SELECT user_group_id, permissions 
            FROM secret_permissions 
            WHERE secret_id = :secret_id
        """),
        {"secret_id": secret.id},
    )
    perms = perms_result.fetchall()

    assert len(perms) == 1
    assert perms[0][0] == 2
    assert set(perms[0][1]) == {"write"}


@mock_aws
@pytest.mark.asyncio
async def test_secret_permissions_multiple_groups_notification_failure(
    pg_session: AsyncSession, secrets_service: SecretsService, test_user_db: User
):
    """Test permission updates with multiple groups when notifications fail partway through."""
    # Create test user
    await _create_user(pg_session, "owner@test.com", "test-user-1")

    # Create a secret
    secret = await secrets_service.create_secret(
        db=pg_session,
        actor=test_user_db,
        data=SecretCreate(
            name="test-secret-3",
            description="Test secret for multiple groups",
            secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
            secret_value="secret-value-789",
        ),
    )
    assert secret is not None

    # Create multiple groups
    for i in range(3, 6):
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, description) 
                VALUES (:id, :name, :desc)
            """),
            {"id": i, "name": f"Test Group {i}", "desc": f"Test group {i}"},
        )
    await pg_session.commit()

    # Mock notification service to fail
    mock_notification_service = AsyncMock()
    mock_notification_service.bulk_create_notifications.side_effect = Exception("Notification batch failed!")
    secrets_service.set_notification_service(mock_notification_service)

    # Update permissions for all three groups
    result = await secrets_service.update_permissions(
        db=pg_session,
        secret_id=secret.id,
        group_permissions=[
            {"user_group_id": 3, "permissions": ["read"]},
            {"user_group_id": 4, "permissions": ["read", "write"]},
            {"user_group_id": 5, "permissions": ["write"]},
        ],
        actor=test_user_db,
        is_admin=False,
    )

    assert result is True

    # Verify ALL permissions were saved
    perms_result = await pg_session.execute(
        text("""
            SELECT user_group_id, permissions 
            FROM secret_permissions 
            WHERE secret_id = :secret_id
            ORDER BY user_group_id
        """),
        {"secret_id": secret.id},
    )
    perms = perms_result.fetchall()

    assert len(perms) == 3
    assert perms[0][0] == 3 and perms[0][1] == ["read"]
    assert perms[1][0] == 4 and set(perms[1][1]) == {"read", "write"}
    assert perms[2][0] == 5 and perms[2][1] == ["write"]
