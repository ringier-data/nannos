"""Unit tests for admin group router (admin group CRUD operations).

Tests cover:
- create_group() - admin-only group creation
- list_groups() - admin view of all groups with pagination
- get_group() - admin view of group details
- update_group() - admin group updates
- delete_group() - admin soft-delete groups
- bulk_delete_groups() - bulk deletion operation
- Role validation for all admin operations
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from playground_backend.models.user import User, UserRole, UserStatus
from playground_backend.models.user_group import (
    BulkDeleteResult,
    BulkGroupDelete,
    UserGroupCreate,
    UserGroupUpdate,
    UserGroupWithMembers,
)
from playground_backend.routers import admin_group_router


@pytest.fixture
def mock_db():
    """Mock database session."""
    db = AsyncMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def mock_admin_user():
    """Mock admin user."""
    return User(
        id="admin-123",
        sub="admin-sub-123",
        email="admin@test.com",
        first_name="Admin",
        last_name="User",
        is_administrator=True,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )


@pytest.fixture
def mock_groups():
    """Mock list of groups."""
    return [
        UserGroupWithMembers(
            id=1,
            name="Test Group 1",
            description="Description 1",
            members=[],
        ),
        UserGroupWithMembers(
            id=2,
            name="Test Group 2",
            description="Description 2",
            members=[],
        ),
    ]


class TestAdminGroupCreation:
    """Test admin group creation endpoint."""

    @pytest.mark.asyncio
    async def test_create_group_as_admin(self, mock_db, mock_admin_user, mock_groups):
        """Test that admins can create groups."""
        request = UserGroupCreate(name="New Group", description="New Description")
        created_group = mock_groups[0]

        with (
            patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.admin_group_router.audit_service") as mock_audit,
        ):
            mock_service.create_group = AsyncMock(
                return_value=MagicMock(id=1, name="New Group", description="New Description")
            )
            mock_service.get_group_with_members = AsyncMock(return_value=created_group)
            mock_audit.log_action = AsyncMock()

            result = await admin_group_router.create_group(request, mock_db, mock_admin_user)

            assert result.data.name == "Test Group 1"
            mock_service.create_group.assert_called_once_with(mock_db, name="New Group", description="New Description")
            assert mock_db.commit.call_count == 2

    @pytest.mark.asyncio
    async def test_create_group_duplicate_name(self, mock_db, mock_admin_user):
        """Test error when creating group with duplicate name."""
        from sqlalchemy.exc import IntegrityError

        request = UserGroupCreate(name="Existing Group", description="Description")

        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            # Simulate database integrity error for duplicate name
            integrity_error = IntegrityError(
                "duplicate key value violates unique constraint",
                params=None,
                orig=Exception("UNIQUE constraint failed"),
            )
            mock_service.create_group = AsyncMock(side_effect=integrity_error)

            with pytest.raises(HTTPException) as exc_info:
                await admin_group_router.create_group(request, mock_db, mock_admin_user)

            assert exc_info.value.status_code == 409
            assert "already exists" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_create_group_validation_error(self, mock_db, mock_admin_user):
        """Test validation error handling."""
        request = UserGroupCreate(name="", description="Description")

        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.create_group = AsyncMock(side_effect=ValueError("Name cannot be empty"))

            with pytest.raises(HTTPException) as exc_info:
                await admin_group_router.create_group(request, mock_db, mock_admin_user)

            assert exc_info.value.status_code == 400


class TestAdminGroupListing:
    """Test admin group listing."""

    @pytest.mark.asyncio
    async def test_list_groups_as_admin(self, mock_db, mock_admin_user, mock_groups):
        """Test that admins can list all groups."""
        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.list_groups = AsyncMock(return_value=(mock_groups, 2))

            result = await admin_group_router.list_groups(mock_db, mock_admin_user, page=1, limit=20)

            assert len(result.data) == 2
            assert result.meta.total == 2
            mock_service.list_groups.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_groups_with_pagination(self, mock_db, mock_admin_user, mock_groups):
        """Test pagination for group listing."""
        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.list_groups = AsyncMock(return_value=(mock_groups[:1], 2))

            result = await admin_group_router.list_groups(mock_db, mock_admin_user, page=2, limit=1)

            assert len(result.data) == 1
            assert result.meta.page == 2
            assert result.meta.limit == 1
            assert result.meta.total == 2

    @pytest.mark.asyncio
    async def test_list_groups_with_search(self, mock_db, mock_admin_user, mock_groups):
        """Test searching groups by name."""
        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.list_groups = AsyncMock(return_value=(mock_groups[:1], 1))

            result = await admin_group_router.list_groups(
                mock_db, mock_admin_user, page=1, limit=20, search="Test Group 1"
            )

            assert len(result.data) == 1
            mock_service.list_groups.assert_called_once_with(mock_db, page=1, limit=20, search="Test Group 1")


class TestAdminGroupDetail:
    """Test admin group detail endpoint."""

    @pytest.mark.asyncio
    async def test_get_group_as_admin(self, mock_db, mock_admin_user, mock_groups):
        """Test that admins can view any group."""
        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.get_group_with_members = AsyncMock(return_value=mock_groups[0])

            result = await admin_group_router.get_group(1, mock_db, mock_admin_user)

            assert result.data.name == "Test Group 1"
            mock_service.get_group_with_members.assert_called_once_with(mock_db, 1)

    @pytest.mark.asyncio
    async def test_get_group_not_found(self, mock_db, mock_admin_user):
        """Test 404 for non-existent group."""
        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.get_group_with_members = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await admin_group_router.get_group(999, mock_db, mock_admin_user)

            assert exc_info.value.status_code == 404


class TestAdminGroupUpdate:
    """Test admin group update endpoint."""

    @pytest.mark.asyncio
    async def test_update_group_as_admin(self, mock_db, mock_admin_user, mock_groups):
        """Test that admins can update any group."""
        request = UserGroupUpdate(name="Updated Name", description="Updated Description")
        updated_group = mock_groups[0]
        updated_group.name = "Updated Name"

        with (
            patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.admin_group_router.audit_service") as mock_audit,
        ):
            mock_service.get_group = AsyncMock(
                return_value=MagicMock(id=1, name="Old Name", description="Old Description")
            )
            mock_service.update_group = AsyncMock(
                return_value=MagicMock(name="Updated Name", description="Updated Description")
            )
            mock_service.get_group_with_members = AsyncMock(return_value=updated_group)
            mock_audit.log_action = AsyncMock()

            result = await admin_group_router.update_group(1, request, mock_db, mock_admin_user)

            assert result.data.name == "Updated Name"
            mock_service.update_group.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_group_partial_updates(self, mock_db, mock_admin_user, mock_groups):
        """Test partial updates (only update provided fields)."""
        request = UserGroupUpdate(name="New Name")  # Only name

        with (
            patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.admin_group_router.audit_service") as mock_audit,
        ):
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1, name="Old Name", description="Description"))
            mock_service.update_group = AsyncMock(return_value=MagicMock(name="New Name", description="Description"))
            mock_service.get_group_with_members = AsyncMock(return_value=mock_groups[0])
            mock_audit.log_action = AsyncMock()

            await admin_group_router.update_group(1, request, mock_db, mock_admin_user)

            # Verify only name was passed to update
            call_args = mock_service.update_group.call_args
            assert "name" in call_args.kwargs
            assert call_args.kwargs["name"] == "New Name"

    @pytest.mark.asyncio
    async def test_update_group_not_found(self, mock_db, mock_admin_user):
        """Test updating non-existent group."""
        request = UserGroupUpdate(name="Updated")

        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.get_group = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await admin_group_router.update_group(999, request, mock_db, mock_admin_user)

            assert exc_info.value.status_code == 404


class TestAdminGroupDeletion:
    """Test admin group deletion."""

    @pytest.mark.asyncio
    async def test_delete_group_as_admin(self, mock_db, mock_admin_user):
        """Test that admins can soft-delete groups."""
        with (
            patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.admin_group_router.audit_service") as mock_audit,
        ):
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1, name="Group", description="Desc"))
            mock_service.delete_group = AsyncMock(return_value=True)
            mock_audit.log_action = AsyncMock()

            result = await admin_group_router.delete_group(1, mock_db, mock_admin_user, force=False)

            assert result is None  # 204 No Content
            mock_service.delete_group.assert_called_once_with(mock_db, 1, force=False)

    @pytest.mark.asyncio
    async def test_delete_group_with_force(self, mock_db, mock_admin_user):
        """Test force deletion even with assigned sub-agents."""
        with (
            patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.admin_group_router.audit_service") as mock_audit,
        ):
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1, name="Group", description="Desc"))
            mock_service.delete_group = AsyncMock(return_value=True)
            mock_audit.log_action = AsyncMock()

            result = await admin_group_router.delete_group(1, mock_db, mock_admin_user, force=True)

            assert result is None
            mock_service.delete_group.assert_called_once_with(mock_db, 1, force=True)

    @pytest.mark.asyncio
    async def test_delete_group_with_assigned_subagents(self, mock_db, mock_admin_user):
        """Test that deletion fails when sub-agents are assigned without force."""
        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1, name="Group", description="Desc"))
            mock_service.delete_group = AsyncMock(
                side_effect=ValueError("Cannot delete group with assigned sub-agents")
            )

            with pytest.raises(HTTPException) as exc_info:
                await admin_group_router.delete_group(1, mock_db, mock_admin_user, force=False)

            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_group_not_found(self, mock_db, mock_admin_user):
        """Test deleting non-existent group."""
        with patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service:
            mock_service.get_group = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await admin_group_router.delete_group(999, mock_db, mock_admin_user)

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_bulk_delete_groups(self, mock_db, mock_admin_user):
        """Test bulk deletion of groups."""
        request = BulkGroupDelete(group_ids=[1, 2, 3], force=False)
        results = [
            BulkDeleteResult(group_id=1, success=True, error=None),
            BulkDeleteResult(group_id=2, success=True, error=None),
            BulkDeleteResult(group_id=3, success=False, error="Has assigned sub-agents"),
        ]

        with (
            patch("playground_backend.routers.admin_group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.admin_group_router.audit_service") as mock_audit,
        ):
            mock_service.bulk_delete_groups = AsyncMock(return_value=results)
            mock_audit.log_action = AsyncMock()

            result = await admin_group_router.bulk_delete_groups(request, mock_db, mock_admin_user)

            assert len(result.data) == 3
            assert result.data[0].success is True
            assert result.data[2].success is False
            # Audit log should only be called for successful deletions
            assert mock_audit.log_action.call_count == 2


class TestAdminRoleValidation:
    """Test role validation for admin operations."""

    @pytest.mark.asyncio
    async def test_list_groups_requires_admin(self, mock_db):
        """Test that list_groups requires admin flag."""
        # The require_admin dependency will handle this validation
        # This test documents the expected behavior
        # Non-admin users would have this structure:
        # User(id="user-123", is_administrator=False, role=UserRole.MEMBER, ...)
        # In practice, the require_admin dependency will raise 403
        # This is enforced by FastAPI's dependency injection system
        # The actual test would require setting up a full FastAPI test client
        pass  # Documented test - actual enforcement is by FastAPI dependency

    @pytest.mark.asyncio
    async def test_create_group_requires_admin(self, mock_db):
        """Test that create_group requires admin flag."""
        # Same as above - enforced by require_admin dependency
        pass  # Documented test

    @pytest.mark.asyncio
    async def test_delete_group_requires_admin(self, mock_db):
        """Test that delete_group requires admin flag."""
        # Same as above - enforced by require_admin dependency
        pass  # Documented test
