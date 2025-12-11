"""Unit tests for group router endpoints (user-facing group operations).

Tests cover:
- list_my_groups() - user's accessible groups
- get_group() - group details with permission check
- list_members() - member list with role information
- add_members() - adding users to groups (manager role required)
- update_member_role() - updating member roles (manager role required)
- remove_member() - removing users from groups (manager role required)
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from playground_backend.models.user import User, UserRole, UserStatus
from playground_backend.models.user_group import (
    MemberInfo,
    UserGroupWithMembers,
)
from playground_backend.routers import group_router


@pytest.fixture
def mock_db():
    """Mock database session."""
    return AsyncMock()


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return User(
        id="user-123",
        sub="user-sub-123",
        email="user@test.com",
        first_name="Test",
        last_name="User",
        is_administrator=False,
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )


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


@pytest.fixture
def mock_members():
    """Mock list of group members."""
    return [
        MemberInfo(
            user_id="user-1",
            email="user1@test.com",
            first_name="User",
            last_name="One",
            group_role="manager",
        ),
        MemberInfo(
            user_id="user-2",
            email="user2@test.com",
            first_name="User",
            last_name="Two",
            group_role="write",
        ),
    ]


class TestGroupListingEndpoints:
    """Test group listing endpoints."""

    @pytest.mark.asyncio
    async def test_list_my_groups_returns_user_groups(self, mock_db, mock_user, mock_groups):
        """Test that list_my_groups returns groups the user belongs to."""
        with patch("playground_backend.routers.group_router.user_group_service") as mock_service:
            mock_service.check_user_permission = AsyncMock(return_value=True)
            mock_service.list_user_groups = AsyncMock(return_value=mock_groups)

            result = await group_router.list_my_groups(mock_db, mock_user)

            assert len(result) == 2
            assert result[0].name == "Test Group 1"
            assert result[1].name == "Test Group 2"
            mock_service.check_user_permission.assert_called_once_with(mock_db, mock_user.id, "groups", "read")
            mock_service.list_user_groups.assert_called_once_with(mock_db, mock_user.id)

    @pytest.mark.asyncio
    async def test_list_my_groups_empty_for_new_user(self, mock_db, mock_user):
        """Test that new users see empty group list."""
        with patch("playground_backend.routers.group_router.user_group_service") as mock_service:
            mock_service.check_user_permission = AsyncMock(return_value=True)
            mock_service.list_user_groups = AsyncMock(return_value=[])

            result = await group_router.list_my_groups(mock_db, mock_user)

            assert len(result) == 0

    @pytest.mark.asyncio
    async def test_list_my_groups_requires_permission(self, mock_db, mock_user):
        """Test that groups.read permission is required."""
        with patch("playground_backend.routers.group_router.user_group_service") as mock_service:
            mock_service.check_user_permission = AsyncMock(return_value=False)

            with pytest.raises(HTTPException) as exc_info:
                await group_router.list_my_groups(mock_db, mock_user)

            assert exc_info.value.status_code == 403
            assert "groups.read required" in exc_info.value.detail


class TestGroupDetailEndpoint:
    """Test get_group endpoint."""

    @pytest.mark.asyncio
    async def test_get_group_returns_details(self, mock_db, mock_user, mock_groups):
        """Test that get_group returns group details."""
        mock_request = MagicMock()
        group = mock_groups[0]

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group_with_members = AsyncMock(return_value=group)
            mock_service.is_group_admin = AsyncMock(return_value=True)

            result = await group_router.get_group(1, mock_request, mock_db, mock_user)

            assert result.data.name == "Test Group 1"
            mock_require.assert_called_once_with(mock_request, 1, mock_db)

    @pytest.mark.asyncio
    async def test_get_group_not_found(self, mock_db, mock_user):
        """Test 404 for non-existent group."""
        mock_request = MagicMock()

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group_with_members = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await group_router.get_group(999, mock_request, mock_db, mock_user)

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_group_hides_members_for_non_admin(self, mock_db, mock_user, mock_groups, mock_members):
        """Test that non-admin members don't see full member list."""
        mock_request = MagicMock()
        group = mock_groups[0]
        group.members = mock_members

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group_with_members = AsyncMock(return_value=group)
            mock_service.is_group_admin = AsyncMock(return_value=False)

            result = await group_router.get_group(1, mock_request, mock_db, mock_user)

            assert len(result.data.members) == 0  # Members hidden


class TestGroupMembersEndpoint:
    """Test list_members endpoint."""

    @pytest.mark.asyncio
    async def test_list_members_returns_member_list(self, mock_db, mock_user, mock_members):
        """Test that list_members returns all group members."""
        mock_request = MagicMock()

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_admin_or_admin") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1, name="Test"))
            mock_service.list_members = AsyncMock(return_value=(mock_members, 2))

            result = await group_router.list_members(1, mock_request, mock_db, mock_user, page=1, limit=20)

            assert len(result.data) == 2
            assert result.meta.total == 2
            mock_require.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_members_pagination(self, mock_db, mock_user, mock_members):
        """Test pagination for member list."""
        mock_request = MagicMock()

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_admin_or_admin") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1))
            mock_service.list_members = AsyncMock(return_value=(mock_members[:1], 2))

            result = await group_router.list_members(1, mock_request, mock_db, mock_user, page=2, limit=1)

            assert len(result.data) == 1
            assert result.meta.page == 2
            assert result.meta.limit == 1


class TestAddMembersEndpoint:
    """Test add_members endpoint."""

    @pytest.mark.asyncio
    async def test_add_members_with_manager_role(self, mock_db, mock_user, mock_members):
        """Test that managers can add members to groups."""
        from playground_backend.models.user_group import GroupMemberAdd

        mock_request = MagicMock()
        request_body = GroupMemberAdd(user_ids=["user-3", "user-4"], role="write")

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member_management_permission") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1))
            mock_service.add_members = AsyncMock(return_value=mock_members)

            result = await group_router.add_members(1, request_body, mock_request, mock_db, mock_user)

            assert len(result.data) == 2
            mock_service.add_members.assert_called_once_with(
                mock_db, actor_sub="user-sub-123", group_id=1, user_ids=["user-3", "user-4"], role="write"
            )

    @pytest.mark.asyncio
    async def test_add_members_group_not_found(self, mock_db, mock_user):
        """Test error when group doesn't exist."""
        from playground_backend.models.user_group import GroupMemberAdd

        mock_request = MagicMock()
        request_body = GroupMemberAdd(user_ids=["user-3"], role="write")

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member_management_permission") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await group_router.add_members(1, request_body, mock_request, mock_db, mock_user)

            assert exc_info.value.status_code == 404


class TestUpdateMemberRoleEndpoint:
    """Test update_member_role endpoint."""

    @pytest.mark.asyncio
    async def test_update_member_role_success(self, mock_db, mock_user):
        """Test successful member role update."""
        from playground_backend.models.user_group import GroupMemberUpdate

        mock_request = MagicMock()
        request_body = GroupMemberUpdate(role="manager")
        updated_member = MemberInfo(
            user_id="user-2",
            email="user2@test.com",
            first_name="User",
            last_name="Two",
            group_role="manager",
        )

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member_management_permission") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1))
            mock_service.update_member_role = AsyncMock(return_value=updated_member)

            result = await group_router.update_member_role(1, "user-2", request_body, mock_request, mock_db, mock_user)

            assert result.group_role == "manager"
            mock_service.update_member_role.assert_called_once_with(
                mock_db, actor_sub="user-sub-123", group_id=1, user_id="user-2", role="manager"
            )

    @pytest.mark.asyncio
    async def test_update_member_role_member_not_found(self, mock_db, mock_user):
        """Test error when member doesn't exist in group."""
        from playground_backend.models.user_group import GroupMemberUpdate

        mock_request = MagicMock()
        request_body = GroupMemberUpdate(role="manager")

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member_management_permission") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1))
            mock_service.update_member_role = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc_info:
                await group_router.update_member_role(1, "user-999", request_body, mock_request, mock_db, mock_user)

            assert exc_info.value.status_code == 404


class TestRemoveMemberEndpoint:
    """Test remove_member endpoint."""

    @pytest.mark.asyncio
    async def test_remove_member_success(self, mock_db, mock_user):
        """Test successful member removal."""
        mock_request = MagicMock()

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member_management_permission") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1))
            mock_service.remove_member = AsyncMock(return_value=True)

            result = await group_router.remove_member(1, "user-2", mock_request, mock_db, mock_user)

            assert result is None  # 204 No Content
            mock_service.remove_member.assert_called_once_with(
                mock_db, actor_sub="user-sub-123", group_id=1, user_id="user-2"
            )

    @pytest.mark.asyncio
    async def test_remove_member_not_found(self, mock_db, mock_user):
        """Test error when member doesn't exist."""
        mock_request = MagicMock()

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member_management_permission") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1))
            mock_service.remove_member = AsyncMock(return_value=False)

            with pytest.raises(HTTPException) as exc_info:
                await group_router.remove_member(1, "user-999", mock_request, mock_db, mock_user)

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_remove_last_manager_prevented(self, mock_db, mock_user):
        """Test that removing the last manager is prevented."""
        mock_request = MagicMock()

        with (
            patch("playground_backend.routers.group_router.user_group_service") as mock_service,
            patch("playground_backend.routers.group_router.require_group_member_management_permission") as mock_require,
        ):
            mock_require.return_value = AsyncMock()
            mock_service.get_group = AsyncMock(return_value=MagicMock(id=1))
            mock_service.remove_member = AsyncMock(side_effect=ValueError("Cannot remove the last manager"))

            with pytest.raises(HTTPException) as exc_info:
                await group_router.remove_member(1, "user-1", mock_request, mock_db, mock_user)

            assert exc_info.value.status_code == 409
            assert "last manager" in exc_info.value.detail.lower()
