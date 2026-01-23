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


import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text

from playground_backend.models.user import User, UserRole, UserStatus
from playground_backend.models.user_group import (
    GroupMemberAdd,
    GroupMemberRemove,
    MemberInfo,
    UserGroupWithMembers,
)
from playground_backend.routers import group_router


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return User(
        id="user-123",
        sub="user-123",
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


@pytest_asyncio.fixture
async def db_test_user(pg_session, mock_user):
    """Setup test environment with app and db."""

    # add user to db
    await pg_session.execute(
        text("""
        INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
        VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
        """),
        {
            "id": mock_user.id,
            "sub": mock_user.sub,
            "email": mock_user.email,
            "first_name": mock_user.first_name,
            "last_name": mock_user.last_name,
            "is_administrator": mock_user.is_administrator,
            "role": mock_user.role,
            "status": mock_user.status,
        },
    )
    await pg_session.commit()


@pytest_asyncio.fixture
async def db_admin_user(pg_session, mock_admin_user):
    """Setup test environment with app and db."""

    # add user to db
    await pg_session.execute(
        text("""
        INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
        VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
        """),
        {
            "id": mock_admin_user.id,
            "sub": mock_admin_user.sub,
            "email": mock_admin_user.email,
            "first_name": mock_admin_user.first_name,
            "last_name": mock_admin_user.last_name,
            "is_administrator": mock_admin_user.is_administrator,
            "role": mock_admin_user.role,
            "status": mock_admin_user.status,
        },
    )
    await pg_session.commit()


@pytest_asyncio.fixture
async def db_test_user_groups(pg_session, mock_user, user_group_service):
    result = await pg_session.execute(text("SELECT * FROM users WHERE id = :user_id"), {"user_id": mock_user.id})
    user = result.first()
    assert user is not None
    await user_group_service.create_group(
        pg_session,
        actor_sub=mock_user.sub,
        name="Test Group 1",
        description="Description 1",
    )
    await user_group_service.create_group(
        pg_session,
        actor_sub=mock_user.sub,
        name="Test Group 2",
        description="Description 2",
    )
    await user_group_service.add_members(
        pg_session,
        actor_sub=mock_user.sub,
        group_id=1,
        user_ids=[mock_user.id],
        role="manager",
    )
    await user_group_service.add_members(
        pg_session,
        actor_sub=mock_user.sub,
        group_id=2,
        user_ids=[mock_user.id],
        role="read",
    )
    await pg_session.commit()

    yield


@pytest.mark.asyncio
class TestGroupListingEndpoints:
    """Test group listing endpoints."""

    @pytest.mark.asyncio
    async def test_list_my_groups_empty_for_new_user(
        self,
        mock_user,
        mock_request,
        pg_session,
        db_test_user,
    ):
        """Test that new users see empty group list."""
        result = await group_router.list_my_groups(mock_request, pg_session, mock_user)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_list_my_groups_returns_user_groups(
        self,
        mock_user,
        mock_request,
        pg_session,
        db_test_user,
        db_test_user_groups,
    ):
        """Test that new users see empty group list."""
        result = await group_router.list_my_groups(mock_request, pg_session, mock_user)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_my_groups_endpoint_returns_user_groups(
        self,
        client_with_db,
        mock_user,
        db_test_user,
        db_test_user_groups,
    ):
        """Test that list_my_groups returns groups the user belongs to."""
        # NOTE: looks like we need to override the dependency again, otherwise it uses a different db session
        client_with_db._transport.app.dependency_overrides[group_router.require_auth] = lambda: mock_user
        response = await client_with_db.get("/api/v1/groups")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["name"] == "Test Group 1"
        assert data[1]["name"] == "Test Group 2"


class TestGroupDetailEndpoint:
    """Test get_group endpoint."""

    @pytest.mark.asyncio
    async def test_get_group_returns_details(
        self, get_mock_request, pg_session, mock_user, db_test_user, db_test_user_groups
    ):
        """Test that get_group returns group details."""
        mock_request = get_mock_request(user=mock_user)
        result = await group_router.get_group(1, mock_request, pg_session, mock_user)

        assert result.data.name == "Test Group 1"

    @pytest.mark.asyncio
    async def test_get_group_not_found(
        self, get_mock_request, pg_session, mock_user, db_test_user, db_test_user_groups
    ):
        """Test 404 for non-existent group."""
        mock_request = get_mock_request(user=mock_user)
        with pytest.raises(HTTPException) as exc_info:
            await group_router.get_group(999, mock_request, pg_session, mock_user)

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_group_members_for_manager(
        self, pg_session, get_mock_request, mock_user, db_test_user, db_test_user_groups
    ):
        """Test that managers see full member list."""
        mock_request = get_mock_request(user=mock_user)
        result = await group_router.get_group(1, mock_request, pg_session, mock_user)
        assert len(result.data.members) == 1  # Members hidden

    @pytest.mark.asyncio
    async def test_get_group_hides_members_for_non_admin(
        self, pg_session, get_mock_request, mock_user, db_test_user, db_test_user_groups
    ):
        """Test that non-admin members don't see full member list."""
        mock_request = get_mock_request(user=mock_user)
        result = await group_router.get_group(2, mock_request, pg_session, mock_user)
        assert len(result.data.members) == 0  # Members hidden


class TestUpdateMemberRoleEndpoint:
    """Test update_member_role endpoint."""

    @pytest.mark.asyncio
    async def test_update_member_role_403(
        self, pg_session, get_mock_request, mock_user, db_test_user, db_test_user_groups
    ):
        """Test member role update forbidden for non-admin/non-manager."""
        from playground_backend.models.user_group import GroupMemberUpdate

        mock_request = get_mock_request(user=mock_user)
        request_body = GroupMemberUpdate(role="manager")
        # mock_request._transport.app.dependency_overrides[require_auth] = lambda: mock_user

        # mock_user.is_administrator = True  # Make user an admin for this test
        with pytest.raises(HTTPException) as exc_info:
            await group_router.update_member_role(2, mock_user.id, mock_request, request_body, pg_session, mock_user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_update_member_role_success_manager(
        self, pg_session, get_mock_request, mock_user, db_test_user, db_test_user_groups
    ):
        """Test member role update forbidden for non-admin/non-manager."""
        from playground_backend.models.user_group import GroupMemberUpdate

        mock_request = get_mock_request(user=mock_user)
        request_body = GroupMemberUpdate(role="manager")
        result = await group_router.update_member_role(
            1, mock_user.id, mock_request, request_body, pg_session, mock_user
        )
        assert result.group_role == "manager"

    @pytest.mark.asyncio
    async def test_update_member_role_success_admin(
        self, pg_session, get_mock_request, mock_user, mock_admin_user, db_test_user, db_admin_user, db_test_user_groups
    ):
        """Test admin can update member roles."""
        from playground_backend.models.user_group import GroupMemberUpdate

        mock_request = get_mock_request(user=mock_admin_user)
        request_body = GroupMemberUpdate(role="manager")
        # admin user updates member role of test user in group 2
        result = await group_router.update_member_role(
            2, mock_user.id, mock_request, request_body, pg_session, mock_admin_user
        )
        assert result.group_role == "manager"

    @pytest.mark.asyncio
    async def test_update_member_role_member_not_found(
        self, pg_session, get_mock_request, mock_user, mock_admin_user, db_test_user, db_admin_user, db_test_user_groups
    ):
        """Test error when member doesn't exist."""
        from playground_backend.models.user_group import GroupMemberUpdate

        mock_request = get_mock_request(user=mock_admin_user)
        request_body = GroupMemberUpdate(role="manager")
        with pytest.raises(HTTPException) as exc_info:
            await group_router.update_member_role(
                1, "user-999", mock_request, request_body, pg_session, mock_admin_user
            )

        assert exc_info.value.status_code == 404


class TestGroupMembersEndpoint:
    """Test list_members endpoint."""

    @pytest.mark.asyncio
    async def test_list_members_returns_member_list(
        self,
        get_mock_request,
        mock_user,
        db_test_user,
        db_test_user_groups,
        pg_session,
    ):
        """Test that list_members returns all group members."""
        # mock_user is manager of group 1
        mock_request = get_mock_request(user=mock_user)
        result = await group_router.list_members(1, mock_request, pg_session, mock_user, page=1, limit=20)
        assert result.meta.total == 1
        assert len(result.data) == 1
        assert result.data[0].user_id == mock_user.id

    @pytest.mark.asyncio
    async def test_list_members_pagination(
        self,
        get_mock_request,
        mock_user,
        db_test_user,
        db_test_user_groups,
        pg_session,
    ):
        """Test pagination for member list (trivial with 1 member)."""
        mock_request = get_mock_request(user=mock_user)
        result = await group_router.list_members(1, mock_request, pg_session, mock_user, page=1, limit=1)
        assert result.meta.page == 1
        assert result.meta.limit == 1
        assert len(result.data) == 1


class TestAddMembersEndpoint:
    """Test add_members endpoint."""

    @pytest.mark.asyncio
    async def test_add_members_with_manager_role(
        self,
        get_mock_request,
        mock_user,
        db_test_user,
        db_test_user_groups,
        pg_session,
    ):
        """Test that managers can add members to groups."""
        from playground_backend.models.user_group import GroupMemberAdd

        # Add two new users to group 1 as manager
        # First, add users to DB
        await pg_session.execute(
            text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
            VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
            """),
            {
                "id": "user-3",
                "sub": "user-3",
                "email": "user3@test.com",
                "first_name": "User",
                "last_name": "Three",
                "is_administrator": False,
                "role": "member",
                "status": "active",
            },
        )
        await pg_session.execute(
            text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
            VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
            """),
            {
                "id": "user-4",
                "sub": "user-4",
                "email": "user4@test.com",
                "first_name": "User",
                "last_name": "Four",
                "is_administrator": False,
                "role": "member",
                "status": "active",
            },
        )
        await pg_session.commit()
        mock_request = get_mock_request(user=mock_user)
        request_body = GroupMemberAdd(user_ids=["user-3", "user-4"], role="write")
        result = await group_router.add_members(1, mock_request, request_body, pg_session, mock_user)
        # Should now be 3 members in group 1
        assert len(result.data) == 3
        user_ids = [m.user_id for m in result.data]
        assert "user-3" in user_ids and "user-4" in user_ids

    @pytest.mark.asyncio
    async def test_add_members_group_not_found(
        self,
        get_mock_request,
        mock_user,
        pg_session,
    ):
        """Test error when group doesn't exist."""
        from playground_backend.models.user_group import GroupMemberAdd

        mock_request = get_mock_request(user=mock_user)
        request_body = GroupMemberAdd(user_ids=["user-999"], role="write")
        with pytest.raises(HTTPException) as exc_info:
            await group_router.add_members(999, mock_request, request_body, pg_session, mock_user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_add_members_group_not_found_admin(
        self,
        get_mock_request,
        mock_admin_user,
        pg_session,
    ):
        """Test error when group doesn't exist."""
        from playground_backend.models.user_group import GroupMemberAdd

        mock_request = get_mock_request(user=mock_admin_user)
        request_body = GroupMemberAdd(user_ids=["user-999"], role="write")
        with pytest.raises(HTTPException) as exc_info:
            await group_router.add_members(999, mock_request, request_body, pg_session, mock_admin_user)
        assert exc_info.value.status_code == 404


class TestRemoveMembersEndpoint:
    """Test remove_member endpoint."""

    @pytest.mark.asyncio
    async def test_remove_member_success(
        self,
        get_mock_request,
        mock_user,
        db_test_user,
        db_test_user_groups,
        pg_session,
    ):
        """Test successful member removal."""
        # Add a new user to group 1, then remove them
        await pg_session.execute(
            text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
            VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
            """),
            {
                "id": "user-5",
                "sub": "user-5",
                "email": "user5@test.com",
                "first_name": "User",
                "last_name": "Five",
                "is_administrator": False,
                "role": "member",
                "status": "active",
            },
        )
        await pg_session.execute(
            text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
            VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
            """),
            {
                "id": "user-6",
                "sub": "user-6",
                "email": "user6@test.com",
                "first_name": "User",
                "last_name": "Six",
                "is_administrator": False,
                "role": "member",
                "status": "active",
            },
        )
        await pg_session.commit()

        mock_request = get_mock_request(user=mock_user)
        add_body = GroupMemberAdd(user_ids=["user-5", "user-6"], role="write")
        await group_router.add_members(1, mock_request, add_body, pg_session, mock_user)
        # Now remove them
        remove_body = GroupMemberRemove(user_ids=["user-5", "user-6"])
        result = await group_router.remove_members(1, mock_request, remove_body, pg_session, mock_user)
        # remaining members should only be mock_user
        assert len(result.data) == 1
        assert result.data[0].user_id == mock_user.id

    @pytest.mark.asyncio
    async def test_remove_last_manager_prevented(
        self,
        get_mock_request,
        mock_user,
        db_test_user,
        db_test_user_groups,
        pg_session,
    ):
        """Test that removing the last manager is prevented."""
        mock_request = get_mock_request(user=mock_user)
        # mock_user is the only manager in group 1
        with pytest.raises(HTTPException) as exc_info:
            await group_router.remove_members(
                1, mock_request, GroupMemberRemove(user_ids=[mock_user.id]), pg_session, mock_user
            )
        assert exc_info.value.status_code == 409
        assert "Cannot remove all managers from a group" in exc_info.value.detail
