"""Unit tests for SecretsService - secret management and access control.

Tests cover:
- Secret creation and SSM parameter generation
- Secret listing with permissions filtering
- Secret retrieval and deletion
- Permission validation for all operations
- Group-based access via secret_permissions table
"""

import os

# Set up boto3 mock environment before any imports that use boto3
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from aiomoto import mock_aws
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.secret import Secret, SecretCreate, SecretType
from playground_backend.services.secrets_service import SecretsService


@mock_aws
async def _create_secret(
    secrets_service: SecretsService,
    session: AsyncSession,
    user_id: str,
    name: str,
    secret_value: str = "test-secret-value",
) -> Secret:
    """Create a test secret and return it."""
    from playground_backend.models.secret import SecretCreate, SecretType

    service = secrets_service

    data = SecretCreate(
        name=name,
        description=f"Test secret: {name}",
        secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
        secret_value=secret_value,
    )

    return await service.create_secret(session, user_id, data)


class TestSecretCreation:
    """Test secret creation and SSM parameter generation."""

    @mock_aws
    @pytest.mark.asyncio
    async def test_create_secret_generates_ssm_parameter(self, pg_session: AsyncSession, secrets_service):
        """Test that creating a secret generates unique SSM parameter name."""
        service = secrets_service
        user_id = await _create_user(pg_session, "owner@test.com", "secret-owner")

        data = SecretCreate(
            name="Test Secret",
            description="Test description",
            secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
            secret_value="secret123",
        )

        secret = await service.create_secret(pg_session, user_id, data)

        assert secret is not None
        assert secret.name == "Test Secret"
        assert secret.secret_type == SecretType.FOUNDRY_CLIENT_SECRET
        assert secret.owner_user_id == user_id
        assert secret.ssm_parameter_name.startswith(service.ssm_vault_prefix)
        assert len(secret.ssm_parameter_name) > len(service.ssm_vault_prefix)

    @mock_aws
    @pytest.mark.asyncio
    async def test_create_secret_unique_name_per_owner(self, pg_session: AsyncSession, secrets_service):
        """Test that secret names must be unique per owner."""
        service = secrets_service
        user_id = await _create_user(pg_session, "owner@test.com", "secret-owner")

        data = SecretCreate(
            name="My Secret",
            secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
            secret_value="value1",
        )

        # Create first secret
        await service.create_secret(pg_session, user_id, data)

        # Try to create second secret with same name
        data2 = SecretCreate(
            name="My Secret",
            secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
            secret_value="value2",
        )

        with pytest.raises(ValueError, match="already exists"):
            await service.create_secret(pg_session, user_id, data2)

    @mock_aws
    @pytest.mark.asyncio
    async def test_different_users_can_have_same_secret_name(self, pg_session: AsyncSession, secrets_service):
        """Test that different users can create secrets with the same name."""
        service = secrets_service
        user1_id = await _create_user(pg_session, "user1@test.com", "secret-user1")
        user2_id = await _create_user(pg_session, "user2@test.com", "secret-user2")

        data = SecretCreate(
            name="Shared Name",
            secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
            secret_value="value1",
        )

        secret1 = await service.create_secret(pg_session, user1_id, data)

        data2 = SecretCreate(
            name="Shared Name",
            secret_type=SecretType.FOUNDRY_CLIENT_SECRET,
            secret_value="value2",
        )

        secret2 = await service.create_secret(pg_session, user2_id, data2)

        assert secret1.name == secret2.name
        assert secret1.owner_user_id != secret2.owner_user_id
        assert secret1.ssm_parameter_name != secret2.ssm_parameter_name


class TestSecretAccessControl:
    @pytest.mark.asyncio
    async def test_owner_has_access_to_own_secret(self, pg_session: AsyncSession, secrets_service):
        """Test that owner always has access to their own secrets (.own)."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Owner Secret"
        )

        has_access = await service.check_user_access(pg_session, secret.id, owner_id, "read", False, False)

        assert has_access is True

    @pytest.mark.asyncio
    async def test_member_cannot_access_other_user_secret(self, pg_session: AsyncSession, secrets_service):
        """Test that members cannot access other users' secrets."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        member_id = await _create_user(pg_session, "member@test.com", "secret-member", role="member")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Owner Secret"
        )

        has_access = await service.check_user_access(pg_session, secret.id, member_id, "read", False, False)

        assert has_access is False

    @pytest.mark.asyncio
    async def test_admin_with_admin_mode_has_access_to_all_secrets(self, pg_session: AsyncSession, secrets_service):
        """Test that admins with admin_mode can access all secrets (.admin)."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        admin_id = await _create_user(pg_session, "admin@test.com", "secret-admin", role="admin")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Owner Secret"
        )

        has_access = await service.check_user_access(pg_session, secret.id, admin_id, "read", False, True)

        assert has_access is True

    @pytest.mark.asyncio
    async def test_admin_without_admin_mode_cannot_access_others_secrets(
        self, pg_session: AsyncSession, secrets_service
    ):
        """Test that admins without admin_mode enabled cannot access others' secrets."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        admin_id = await _create_user(pg_session, "admin@test.com", "secret-admin", role="admin")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Owner Secret"
        )

        has_access = await service.check_user_access(pg_session, secret.id, admin_id, "read", False, False)

        assert has_access is False

    @pytest.mark.asyncio
    async def test_group_access_with_secret_permissions(self, pg_session: AsyncSession, secrets_service):
        """Test that users can access secrets granted to their groups."""
        from sqlalchemy import text

        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        admin_id = await _create_user(pg_session, "admin@test.com", "secret-admin", role="admin")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Shared Secret"
        )

        # Create a group and add admin to it
        group_result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Test Group', 'Test group', NOW(), NOW())
                RETURNING id
            """)
        )
        group_id = group_result.scalar_one()

        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'write', NOW())
            """),
            {"group_id": group_id, "user_id": admin_id},
        )

        # Grant group read permission on the secret
        await pg_session.execute(
            text("""
                INSERT INTO secret_permissions (secret_id, user_group_id, permissions, created_at)
                VALUES (:secret_id, :group_id, ARRAY['read'], NOW())
            """),
            {"secret_id": secret.id, "group_id": group_id},
        )
        await pg_session.commit()

        # Admin with admin_mode and .group capability can access
        has_access = await service.check_user_access(pg_session, secret.id, admin_id, "read", False, False)

        assert has_access is True

    @pytest.mark.asyncio
    async def test_group_access_requires_admin_mode(self, pg_session: AsyncSession, secrets_service):
        """Test that group-based access requires admin_mode to be enabled."""
        from sqlalchemy import text

        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        admin_id = await _create_user(pg_session, "admin@test.com", "secret-admin", role="admin")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Shared Secret"
        )

        # Create group, add admin, grant permissions
        group_result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Test Group', 'Test group', NOW(), NOW())
                RETURNING id
            """)
        )
        group_id = group_result.scalar_one()

        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'write', NOW())
            """),
            {"group_id": group_id, "user_id": admin_id},
        )

        await pg_session.execute(
            text("""
                INSERT INTO secret_permissions (secret_id, user_group_id, permissions, created_at)
                VALUES (:secret_id, :group_id, ARRAY['read'], NOW())
            """),
            {"secret_id": secret.id, "group_id": group_id},
        )
        await pg_session.commit()

        # Without admin_mode, even with group access, user cannot access
        has_access = await service.check_user_access(pg_session, secret.id, admin_id, False, False)

        assert has_access is False

    @pytest.mark.asyncio
    async def test_member_cannot_use_group_access(self, pg_session: AsyncSession, secrets_service):
        """Test that members don't have .group capability even with group membership."""
        from sqlalchemy import text

        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        member_id = await _create_user(pg_session, "member@test.com", "secret-member", role="member")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Shared Secret"
        )

        # Create group, add member, grant permissions
        group_result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Test Group', 'Test group', NOW(), NOW())
                RETURNING id
            """)
        )
        group_id = group_result.scalar_one()

        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'write', NOW())
            """),
            {"group_id": group_id, "user_id": member_id},
        )

        await pg_session.execute(
            text("""
                INSERT INTO secret_permissions (secret_id, user_group_id, permissions, created_at)
                VALUES (:secret_id, :group_id, ARRAY['read'], NOW())
            """),
            {"secret_id": secret.id, "group_id": group_id},
        )
        await pg_session.commit()

        # Members don't have .group capability, so cannot access
        has_access = await service.check_user_access(pg_session, secret.id, member_id, False, True)

        assert has_access is False


class TestSecretRetrieval:
    """Test secret retrieval and value fetching from SSM."""

    @pytest.mark.asyncio
    async def test_get_secret_returns_metadata(self, pg_session: AsyncSession, secrets_service):
        """Test that get_secret returns secret metadata."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Test Secret"
        )

        retrieved = await service.get_secret(pg_session, secret.id, owner_id, False, False)

        assert retrieved is not None
        assert retrieved.id == secret.id
        assert retrieved.name == "Test Secret"
        assert retrieved.owner_user_id == owner_id
        assert retrieved.ssm_parameter_name == secret.ssm_parameter_name

    @pytest.mark.asyncio
    async def test_get_secret_checks_access_permission(self, pg_session: AsyncSession, secrets_service):
        """Test that get_secret validates access permissions."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        other_id = await _create_user(pg_session, "other@test.com", "secret-other")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Private Secret"
        )

        # Other user cannot access (returns None)
        retrieved = await service.get_secret(pg_session, secret.id, other_id, False, False)
        assert retrieved is None

    @mock_aws
    @pytest.mark.asyncio
    async def test_get_secret_value_retrieves_from_ssm(self, pg_session: AsyncSession, secrets_service):
        """Test that get_secret_value retrieves value from SSM Parameter Store."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        secret = await _create_secret(
            secrets_service=secrets_service,
            session=pg_session,
            user_id=owner_id,
            name="Test Secret",
            secret_value="my-secret-value",
        )

        value = await service.get_secret_value(pg_session, secret.id, owner_id, False, False)

        assert value == "my-secret-value"

    @pytest.mark.asyncio
    async def test_get_secret_value_checks_access_permission(self, pg_session: AsyncSession, secrets_service):
        """Test that get_secret_value validates access permissions."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        other_id = await _create_user(pg_session, "other@test.com", "secret-other")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Private Secret"
        )

        # Other user cannot access
        with pytest.raises(PermissionError, match="permission"):
            await service.get_secret_value(pg_session, secret.id, other_id, False, False)


# Note: list_secrets method not yet implemented in SecretsService
# These tests are placeholders for when the method is added


class TestSecretDeletion:
    """Test secret deletion and SSM parameter cleanup."""

    @mock_aws
    @pytest.mark.asyncio
    async def test_delete_secret_soft_deletes_record(self, pg_session: AsyncSession, secrets_service):
        """Test that delete_secret performs soft delete."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Test Secret"
        )

        result = await service.delete_secret(pg_session, secret.id, owner_id, False, False)

        assert result is True

        # Verify soft delete (record still exists but marked deleted)
        from sqlalchemy import text

        check = await pg_session.execute(text("SELECT deleted_at FROM secrets WHERE id = :id"), {"id": secret.id})
        row = check.fetchone()
        assert row is not None
        assert row[0] is not None  # deleted_at is set

    @mock_aws
    @pytest.mark.asyncio
    async def test_delete_secret_removes_ssm_parameter(self, pg_session: AsyncSession, secrets_service):
        """Test that delete_secret removes SSM parameter."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Test Secret"
        )

        await service.delete_secret(pg_session, secret.id, owner_id, False, False)

        # Try to get parameter - should fail
        async with service.session.create_client("ssm", region_name=service.region_name) as ssm_client:
            from botocore.exceptions import ClientError

            with pytest.raises(ClientError):
                await ssm_client.get_parameter(Name=secret.ssm_parameter_name)

    @pytest.mark.asyncio
    async def test_delete_secret_checks_permission(self, pg_session: AsyncSession, secrets_service):
        """Test that only authorized users can delete secrets."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        other_id = await _create_user(pg_session, "other@test.com", "secret-other")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Private Secret"
        )

        # Other user cannot delete
        with pytest.raises(PermissionError, match="permission"):
            await service.delete_secret(pg_session, secret.id, other_id, False, False)

    @mock_aws
    @pytest.mark.asyncio
    async def test_admin_can_delete_any_secret_with_admin_mode(self, pg_session: AsyncSession, secrets_service):
        """Test that admins with admin_mode can delete any secret."""
        service = secrets_service
        owner_id = await _create_user(pg_session, "owner@test.com", "secret-owner")
        admin_id = await _create_user(pg_session, "admin@test.com", "secret-admin", role="admin")
        secret = await _create_secret(
            secrets_service=secrets_service, session=pg_session, user_id=owner_id, name="Owner Secret"
        )

        result = await service.delete_secret(pg_session, secret.id, admin_id, False, True)

        assert result is True


# Note: update_secret_value method not yet implemented in SecretsService
# These tests are placeholders for when the method is added


# Helper methods for test fixtures
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
    from sqlalchemy import text

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
