"""Tests for UserGroupService with Keycloak integration and transaction handling."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from playground_backend.repositories.user_group_repository import UserGroupRepository
from playground_backend.services.keycloak_admin_service import KeycloakSyncError
from playground_backend.services.user_group_service import UserGroupService


@pytest_asyncio.fixture
async def mock_keycloak_service():
    """Create a mocked KeycloakAdminService."""
    mock = AsyncMock()
    mock.create_group = AsyncMock(return_value="kc-group-123")
    mock.update_group = AsyncMock()
    mock.delete_group = AsyncMock()
    mock.add_user_to_group = AsyncMock()
    mock.remove_user_from_group = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def user_group_service_with_keycloak(mock_keycloak_service):
    """Create UserGroupService with mocked Keycloak and audit service."""
    from playground_backend.services.audit_service import AuditService

    repo = UserGroupRepository()
    # Mock audit service to avoid DynamoDB calls
    mock_audit = AsyncMock(spec=AuditService)
    repo.set_audit_service(mock_audit)

    service = UserGroupService(user_group_repository=repo, keycloak_admin_service=mock_keycloak_service)
    return service


class TestCreateGroupWithKeycloakTransaction:
    """Test create_group with Keycloak sync and transaction handling."""

    @pytest.mark.asyncio
    async def test_create_group_success_syncs_to_keycloak(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test successful group creation syncs to Keycloak and stores group ID."""
        service = user_group_service_with_keycloak

        # Create group
        group = await service.create_group(
            db=pg_session,
            actor_sub="actor-123",
            name="Test Group",
            description="Test Description",
        )
        await pg_session.commit()

        # Verify Keycloak was called
        mock_keycloak_service.create_group.assert_called_once_with("Test Group", "Test Description")

        # Verify database has Keycloak group ID
        result = await pg_session.execute(
            text("SELECT keycloak_group_id FROM user_groups WHERE id = :id"), {"id": group.id}
        )
        row = result.mappings().first()
        assert row["keycloak_group_id"] == "kc-group-123"

    @pytest.mark.asyncio
    async def test_create_group_keycloak_failure_rolls_back_db(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test Keycloak failure during create rolls back database transaction."""
        service = user_group_service_with_keycloak

        # Mock Keycloak failure
        mock_keycloak_service.create_group.side_effect = KeycloakSyncError("Keycloak is down")

        # Attempt to create group - exception should be raised
        with pytest.raises(KeycloakSyncError):
            await service.create_group(
                db=pg_session,
                actor_sub="actor-123",
                name="Failing Group",
                description="Should not persist",
            )

        # Explicitly rollback the transaction (in real code, controller would do this)
        await pg_session.rollback()

        # Verify group was NOT persisted in database
        result = await pg_session.execute(
            text("SELECT COUNT(*) as count FROM user_groups WHERE name = :name"), {"name": "Failing Group"}
        )
        count = result.scalar()
        assert count == 0


class TestUpdateGroupWithKeycloakTransaction:
    """Test update_group with Keycloak sync and transaction handling."""

    @pytest.mark.asyncio
    async def test_update_group_success_syncs_to_keycloak(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test successful group update syncs to Keycloak."""
        service = user_group_service_with_keycloak

        # Create group first
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, description, keycloak_group_id, created_at, updated_at)
                VALUES (1, 'Original Name', 'Original Desc', 'kc-123', NOW(), NOW())
            """)
        )
        await pg_session.commit()

        # Update group
        updated_group = await service.update_group(
            db=pg_session,
            actor_sub="actor-123",
            group_id=1,
            name="Updated Name",
            description="Updated Desc",
        )
        await pg_session.commit()

        # Verify Keycloak was called
        mock_keycloak_service.update_group.assert_called_once_with("kc-123", "Updated Name", "Updated Desc")

        # Verify database was updated
        assert updated_group.name == "Updated Name"
        assert updated_group.description == "Updated Desc"

    @pytest.mark.asyncio
    async def test_update_group_keycloak_failure_rolls_back_db(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test Keycloak failure during update rolls back database transaction."""
        service = user_group_service_with_keycloak

        # Create group first
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, description, keycloak_group_id, created_at, updated_at)
                VALUES (1, 'Original Name', 'Original Desc', 'kc-123', NOW(), NOW())
            """)
        )
        await pg_session.commit()

        # Mock Keycloak failure
        mock_keycloak_service.update_group.side_effect = KeycloakSyncError("Keycloak is down")

        # Attempt to update group - exception should be raised
        with pytest.raises(KeycloakSyncError):
            await service.update_group(
                db=pg_session,
                actor_sub="actor-123",
                group_id=1,
                name="Should Not Update",
                description="Should Not Update",
            )

        # Explicitly rollback the transaction (in real code, controller would do this)
        await pg_session.rollback()

        # Verify database was NOT updated
        result = await pg_session.execute(text("SELECT name, description FROM user_groups WHERE id = 1"))
        row = result.mappings().first()
        assert row["name"] == "Original Name"
        assert row["description"] == "Original Desc"


class TestDeleteGroupWithKeycloakTransaction:
    """Test delete_group with Keycloak sync and transaction handling."""

    @pytest.mark.asyncio
    async def test_delete_group_success_syncs_to_keycloak(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test successful group deletion syncs to Keycloak."""
        service = user_group_service_with_keycloak

        # Create group first
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, description, keycloak_group_id, created_at, updated_at)
                VALUES (1, 'Group To Delete', 'Desc', 'kc-123', NOW(), NOW())
            """)
        )
        await pg_session.commit()

        # Delete group
        success = await service.delete_group(
            db=pg_session,
            actor_sub="actor-123",
            group_id=1,
        )
        await pg_session.commit()

        # Verify Keycloak was called
        mock_keycloak_service.delete_group.assert_called_once_with("kc-123")

        # Verify database was soft-deleted
        assert success is True
        result = await pg_session.execute(text("SELECT deleted_at FROM user_groups WHERE id = 1"))
        row = result.mappings().first()
        assert row["deleted_at"] is not None

    @pytest.mark.asyncio
    async def test_delete_group_keycloak_failure_rolls_back_db(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test Keycloak failure during delete rolls back database transaction."""
        service = user_group_service_with_keycloak

        # Create group first
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, description, keycloak_group_id, created_at, updated_at)
                VALUES (1, 'Group To Delete', 'Desc', 'kc-123', NOW(), NOW())
            """)
        )
        await pg_session.commit()

        # Mock Keycloak failure
        mock_keycloak_service.delete_group.side_effect = KeycloakSyncError("Keycloak is down")

        # Attempt to delete group - exception should be raised
        with pytest.raises(KeycloakSyncError):
            await service.delete_group(
                db=pg_session,
                actor_sub="actor-123",
                group_id=1,
            )

        # Explicitly rollback the transaction (in real code, controller would do this)
        await pg_session.rollback()

        # Verify database was NOT soft-deleted
        result = await pg_session.execute(text("SELECT deleted_at FROM user_groups WHERE id = 1"))
        row = result.mappings().first()
        assert row["deleted_at"] is None


class TestAddMembersWithKeycloakTransaction:
    """Test add_members with Keycloak sync and transaction handling."""

    @pytest.mark.asyncio
    async def test_add_members_success_syncs_to_keycloak(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test successful member addition syncs to Keycloak sequentially."""
        service = user_group_service_with_keycloak

        # Create group and users
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, keycloak_group_id, created_at, updated_at)
                VALUES (1, 'Test Group', 'kc-group-123', NOW(), NOW())
            """)
        )
        await pg_session.execute(
            text("""
                INSERT INTO users (id, sub, email, first_name, last_name, role, status, created_at, updated_at)
                VALUES 
                    ('sub-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active', NOW(), NOW()),
                    ('sub-2', 'sub-2', 'user2@test.com', 'User', 'Two', 'member', 'active', NOW(), NOW())
            """)
        )
        await pg_session.commit()

        # Add members
        members = await service.add_members(
            db=pg_session,
            actor_sub="actor-123",
            group_id=1,
            user_ids=["sub-1", "sub-2"],
            role="read",
        )
        await pg_session.commit()

        # Verify Keycloak was called twice (sequential)
        assert mock_keycloak_service.add_user_to_group.call_count == 2
        mock_keycloak_service.add_user_to_group.assert_any_call("sub-1", "kc-group-123")
        mock_keycloak_service.add_user_to_group.assert_any_call("sub-2", "kc-group-123")

        # Verify database has members
        assert len(members) == 2

    @pytest.mark.asyncio
    async def test_add_members_keycloak_failure_rolls_back_db(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test Keycloak failure during add_members rolls back database transaction."""
        service = user_group_service_with_keycloak

        # Create group and users
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, keycloak_group_id, created_at, updated_at)
                VALUES (1, 'Test Group', 'kc-group-123', NOW(), NOW())
            """)
        )
        await pg_session.execute(
            text("""
                INSERT INTO users (id, sub, email, first_name, last_name, role, status, created_at, updated_at)
                VALUES ('sub-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active', NOW(), NOW())
            """)
        )
        await pg_session.commit()

        # Mock Keycloak failure
        mock_keycloak_service.add_user_to_group.side_effect = KeycloakSyncError("Keycloak is down")

        # Attempt to add members - exception should be raised
        with pytest.raises(KeycloakSyncError):
            await service.add_members(
                db=pg_session,
                actor_sub="actor-123",
                group_id=1,
                user_ids=["sub-1"],
                role="read",
            )

        # Explicitly rollback the transaction (in real code, controller would do this)
        await pg_session.rollback()

        # Verify members were NOT added to database
        result = await pg_session.execute(
            text("SELECT COUNT(*) as count FROM user_group_members WHERE user_group_id = 1")
        )
        count = result.scalar()
        assert count == 0


class TestRemoveMemberWithKeycloakTransaction:
    """Test remove_member with Keycloak sync and transaction handling."""

    @pytest.mark.asyncio
    async def test_remove_member_success_syncs_to_keycloak(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test successful member removal syncs to Keycloak."""
        service = user_group_service_with_keycloak

        # Create group, user, and membership
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, keycloak_group_id, created_at, updated_at)
                VALUES (1, 'Test Group', 'kc-group-123', NOW(), NOW())
            """)
        )
        await pg_session.execute(
            text("""
                INSERT INTO users (id, sub, email, first_name, last_name, role, status, created_at, updated_at)
                VALUES ('sub-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active', NOW(), NOW())
            """)
        )
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (id, user_id, user_group_id, group_role, created_at)
                VALUES (1, 'sub-1', 1, 'read', NOW())
            """)
        )
        await pg_session.commit()

        # Remove member
        success = await service.remove_member(
            db=pg_session,
            actor_sub="actor-123",
            group_id=1,
            user_id="sub-1",
        )
        await pg_session.commit()

        # Verify Keycloak was called
        mock_keycloak_service.remove_user_from_group.assert_called_once_with("sub-1", "kc-group-123")

        # Verify database removed member
        assert success is True
        result = await pg_session.execute(
            text("SELECT COUNT(*) as count FROM user_group_members WHERE user_id = 'sub-1'")
        )
        count = result.scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_remove_member_keycloak_failure_rolls_back_db(
        self, pg_session, user_group_service_with_keycloak, mock_keycloak_service
    ):
        """Test Keycloak failure during remove_member rolls back database transaction."""
        service = user_group_service_with_keycloak

        # Create group, user, and membership
        await pg_session.execute(
            text("""
                INSERT INTO user_groups (id, name, keycloak_group_id, created_at, updated_at)
                VALUES (1, 'Test Group', 'kc-group-123', NOW(), NOW())
            """)
        )
        await pg_session.execute(
            text("""
                INSERT INTO users (id, sub, email, first_name, last_name, role, status, created_at, updated_at)
                VALUES ('sub-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active', NOW(), NOW())
            """)
        )
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (id, user_id, user_group_id, group_role, created_at)
                VALUES (1, 'sub-1', 1, 'read', NOW())
            """)
        )
        await pg_session.commit()

        # Mock Keycloak failure
        mock_keycloak_service.remove_user_from_group.side_effect = KeycloakSyncError("Keycloak is down")

        # Attempt to remove member - should raise exception and NOT commit
        with pytest.raises(KeycloakSyncError):
            await service.remove_member(
                db=pg_session,
                actor_sub="actor-123",
                group_id=1,
                user_id="sub-1",
            )

        # Explicitly rollback the transaction (in real code, controller would do this)
        await pg_session.rollback()

        # Verify member was NOT removed from database
        result = await pg_session.execute(
            text("SELECT COUNT(*) as count FROM user_group_members WHERE user_id = 'sub-1'")
        )
        count = result.scalar()
        assert count == 1
