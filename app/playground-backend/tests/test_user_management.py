"""Tests for User Management API (UserService and UserGroupService).

These tests use the PostgreSQL fixtures with Rambler migrations to ensure
schema parity with production.
"""

import pytest
import pytest_asyncio

from playground_backend.models.user import UserStatus
from playground_backend.services.user_group_service import UserGroupService
from playground_backend.services.user_service import UserService


@pytest.fixture
def user_service():
    """Create UserService instance."""
    return UserService()


@pytest.fixture
def user_group_service():
    """Create UserGroupService instance."""
    return UserGroupService()


# Alias pg_session to db_session for compatibility with tests
@pytest_asyncio.fixture
async def db_session(pg_session):
    """Alias for pg_session to match test expectations."""
    yield pg_session


@pytest.mark.asyncio
class TestUserServiceExtended:
    """Test extended UserService functionality."""

    async def test_list_users_empty(self, user_service, db_session):
        """Test listing users when no users exist."""
        users, total = await user_service.list_users(db_session)

        assert users == []
        assert total == 0

    async def test_list_users_with_data(self, user_service, db_session):
        """Test listing users with pagination."""
        # Create multiple users
        for i in range(5):
            await user_service.upsert_user(
                db=db_session,
                sub=f"user-{i}",
                email=f"user{i}@example.com",
                first_name=f"User{i}",
                last_name="Test",
            )

        # Get first page
        users, total = await user_service.list_users(db_session, page=1, limit=2)

        assert len(users) == 2
        assert total == 5

        # Get second page
        users, total = await user_service.list_users(db_session, page=2, limit=2)

        assert len(users) == 2
        assert total == 5

    async def test_list_users_exclude_deleted(self, user_service, db_session):
        """Test that deleted users are excluded by default."""
        # Create active user
        await user_service.upsert_user(
            db=db_session,
            sub="active-user",
            email="active@example.com",
            first_name="Active",
            last_name="User",
        )

        # Create user and then delete them
        await user_service.upsert_user(
            db=db_session,
            sub="deleted-user",
            email="deleted@example.com",
            first_name="Deleted",
            last_name="User",
        )
        await user_service.update_user_status(
            db=db_session, user_id="deleted-user", actor_sub="test-actor", status=UserStatus.DELETED
        )

        # By default, deleted users are excluded
        users, total = await user_service.list_users(db_session)

        assert total == 1
        assert users[0].sub == "active-user"

        # With include_deleted=True, all users are returned
        users, total = await user_service.list_users(db_session, include_deleted=True)
        assert total == 2

    async def test_list_users_search_by_email(self, user_service, db_session):
        """Test searching users by email."""
        await user_service.upsert_user(
            db=db_session,
            sub="user-1",
            email="john.doe@example.com",
            first_name="John",
            last_name="Doe",
        )
        await user_service.upsert_user(
            db=db_session,
            sub="user-2",
            email="jane.smith@example.com",
            first_name="Jane",
            last_name="Smith",
        )

        users, total = await user_service.list_users(db_session, search="john")

        assert total == 1
        assert users[0].email == "john.doe@example.com"

    async def test_list_users_search_by_name(self, user_service, db_session):
        """Test searching users by name."""
        await user_service.upsert_user(
            db=db_session,
            sub="user-1",
            email="john@example.com",
            first_name="John",
            last_name="Doe",
        )
        await user_service.upsert_user(
            db=db_session,
            sub="user-2",
            email="jane@example.com",
            first_name="Jane",
            last_name="Smith",
        )

        users, total = await user_service.list_users(db_session, search="smith")

        assert total == 1
        assert users[0].last_name == "Smith"

    async def test_update_user_status(self, user_service, db_session):
        """Test updating user status."""
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Suspend user
        user = await user_service.update_user_status(
            db=db_session, user_id="test-user", actor_sub="test-actor", status=UserStatus.SUSPENDED
        )

        assert user is not None
        assert user.status == UserStatus.SUSPENDED

    async def test_update_user_status_deleted_sets_deleted_at(self, user_service, db_session):
        """Test that setting status to deleted also sets deleted_at."""
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        user = await user_service.update_user_status(
            db=db_session, user_id="test-user", actor_sub="test-actor", status=UserStatus.DELETED
        )

        assert user is not None
        assert user.status == UserStatus.DELETED
        assert user.deleted_at is not None

    async def test_update_user_status_not_found(self, user_service, db_session):
        """Test updating status of non-existent user."""
        user = await user_service.update_user_status(
            db=db_session, user_id="non-existent", actor_sub="test-actor", status=UserStatus.SUSPENDED
        )

        assert user is None

    async def test_get_user_with_groups(self, user_service, user_group_service: UserGroupService, db_session):
        """Test getting user with their group memberships."""
        # Create user
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Create group and add user
        group = await user_group_service.create_group(
            db=db_session,
            name="Test Group",
            description="A test group",
            actor_sub="test-actor",
        )

        await user_group_service.add_members(
            db=db_session,
            actor_sub="test-actor",
            group_id=group.id,
            user_ids=["test-user"],
            role="read",
        )

        # Get user with groups
        user = await user_service.get_user_with_groups(db_session, "test-user")

        assert user is not None
        assert len(user.groups) == 1
        assert user.groups[0].group_id == group.id
        assert user.groups[0].group_name == "Test Group"
        assert user.groups[0].group_role == "read"

    async def test_update_user_groups(self, user_service, user_group_service: UserGroupService, db_session):
        """Test updating user's group memberships."""
        # Create user
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Create groups
        group1 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Group 1")
        group2 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Group 2")

        # Initially add user to group1
        await user_group_service.add_members(
            db=db_session, group_id=group1.id, user_ids=["test-user"], role="read", actor_sub="test-actor"
        )

        # Update to only group2 using 'set' operation
        result = await user_service.update_user_groups(
            db=db_session,
            user_id="test-user",
            actor_sub="test-actor",
            group_ids=[group2.id],
            operation="set",
        )

        assert result is not None

        # Verify user is now in group2 only
        user = await user_service.get_user_with_groups(db_session, "test-user")
        assert len(user.groups) == 1
        assert user.groups[0].group_id == group2.id

    async def test_bulk_update_users(self, user_service, db_session):
        """Test bulk updating multiple users."""
        from playground_backend.models.user import BulkUserOperation

        # Create users
        for i in range(3):
            await user_service.upsert_user(
                db=db_session,
                sub=f"user-{i}",
                email=f"user{i}@example.com",
                first_name=f"User{i}",
                last_name="Test",
            )

        # Bulk suspend users
        operations = [
            BulkUserOperation(user_id="user-0", action="suspend"),
            BulkUserOperation(user_id="user-1", action="suspend"),
            BulkUserOperation(user_id="user-2", action="suspend"),
        ]
        results = await user_service.bulk_update_users(
            db=db_session,
            actor_sub="test-actor",
            operations=operations,
        )

        assert all(r.success for r in results)
        assert len(results) == 3

        # Verify users are suspended by checking their status individually
        user0 = await user_service.get_user(db_session, "user-0")
        assert user0.status == UserStatus.SUSPENDED

    async def test_bulk_update_users_partial_failure(self, user_service, db_session):
        """Test bulk update with some non-existent users."""
        from playground_backend.models.user import BulkUserOperation

        await user_service.upsert_user(
            db=db_session,
            sub="user-1",
            email="user1@example.com",
            first_name="User1",
            last_name="Test",
        )

        operations = [
            BulkUserOperation(user_id="user-1", action="suspend"),
            BulkUserOperation(user_id="non-existent", action="suspend"),
        ]
        results = await user_service.bulk_update_users(
            db=db_session,
            actor_sub="test-actor",
            operations=operations,
        )

        # Find results by user_id
        result_map = {r.user_id: r for r in results}
        assert result_map["user-1"].success is True
        assert result_map["non-existent"].success is False


@pytest.mark.asyncio
class TestUserGroupService:
    """Test UserGroupService functionality."""

    async def test_create_group(self, user_group_service: UserGroupService, db_session):
        """Test creating a new group."""
        group = await user_group_service.create_group(
            db=db_session,
            name="Engineering",
            actor_sub="test-actor",
            description="Engineering team",
        )

        assert group is not None
        assert group.name == "Engineering"
        assert group.description == "Engineering team"
        assert group.deleted_at is None

    async def test_create_group_duplicate_name(self, user_group_service: UserGroupService, db_session):
        """Test creating a group with duplicate name fails."""
        from sqlalchemy.exc import IntegrityError

        await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Unique Name")

        # The service raises the underlying IntegrityError (or it may raise ValueError)
        with pytest.raises((IntegrityError, ValueError)):
            await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Unique Name")

    async def test_get_group(self, user_group_service: UserGroupService, db_session):
        """Test getting a group by ID."""
        created = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        group = await user_group_service.get_group(db_session, created.id)

        assert group is not None
        assert group.id == created.id
        assert group.name == "Test Group"

    async def test_get_group_not_found(self, user_group_service: UserGroupService, db_session):
        """Test getting a non-existent group."""
        group = await user_group_service.get_group(db_session, 99999)

        assert group is None

    async def test_get_group_excludes_deleted(self, user_group_service: UserGroupService, db_session):
        """Test that deleted groups are not returned by get_group."""
        created = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        await user_group_service.delete_group(db_session, group_id=created.id, actor_sub="test-actor")

        group = await user_group_service.get_group(db_session, created.id)
        assert group is None

    async def test_list_groups(self, user_group_service: UserGroupService, db_session):
        """Test listing groups with pagination."""
        for i in range(5):
            await user_group_service.create_group(db=db_session, actor_sub="test-actor", name=f"Group {i}")

        groups, total = await user_group_service.list_groups(db_session, page=1, limit=2)

        assert len(groups) == 2
        assert total == 5

    async def test_list_groups_excludes_deleted(self, user_group_service: UserGroupService, db_session):
        """Test that deleted groups are excluded from listing."""
        group1 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Active Group")
        group2 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Deleted Group")

        await user_group_service.delete_group(db_session, group_id=group2.id, actor_sub="test-actor")

        groups, total = await user_group_service.list_groups(db_session)

        assert total == 1
        assert groups[0].id == group1.id

    async def test_update_group(self, user_group_service: UserGroupService, db_session):
        """Test updating a group."""
        created = await user_group_service.create_group(
            db=db_session,
            name="Original Name",
            description="Original description",
            actor_sub="test-actor",
        )

        updated = await user_group_service.update_group(
            db=db_session,
            group_id=created.id,
            actor_sub="test-actor",
            name="Updated Name",
            description="Updated description",
        )

        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.description == "Updated description"

    async def test_update_group_partial(self, user_group_service: UserGroupService, db_session):
        """Test partially updating a group."""
        created = await user_group_service.create_group(
            db=db_session,
            actor_sub="test-actor",
            name="Original Name",
            description="Original description",
        )

        # Only update name
        updated = await user_group_service.update_group(
            db=db_session, actor_sub="test-actor", group_id=created.id, name="New Name"
        )

        assert updated is not None
        assert updated.name == "New Name"
        assert updated.description == "Original description"  # Unchanged

    async def test_delete_group_soft_delete(self, user_group_service: UserGroupService, db_session):
        """Test that delete performs soft delete."""
        created = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        result = await user_group_service.delete_group(db_session, group_id=created.id, actor_sub="test-actor")

        assert result is True

        # Group should not be visible
        group = await user_group_service.get_group(db_session, created.id)
        assert group is None

    async def test_add_member(self, user_service, user_group_service: UserGroupService, db_session):
        """Test adding a member to a group."""
        # Create user
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Create group
        group = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        # Add member
        members = await user_group_service.add_members(
            db=db_session,
            group_id=group.id,
            actor_sub="test-actor",
            user_ids=["test-user"],
            role="read",
        )

        assert len(members) == 1
        assert members[0].user_id == "test-user"
        assert members[0].group_role == "read"

    async def test_add_member_as_admin(self, user_service, user_group_service: UserGroupService, db_session):
        """Test adding a member with admin role."""
        await user_service.upsert_user(
            db=db_session,
            sub="admin-user",
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
        )

        group = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        members = await user_group_service.add_members(
            db=db_session,
            group_id=group.id,
            actor_sub="test-actor",
            user_ids=["admin-user"],
            role="manager",
        )

        assert len(members) == 1
        assert members[0].group_role == "manager"

    async def test_add_multiple_members(self, user_service, user_group_service: UserGroupService, db_session):
        """Test adding multiple members at once."""
        await user_service.upsert_user(
            db=db_session,
            sub="user-1",
            email="user1@example.com",
            first_name="User",
            last_name="One",
        )
        await user_service.upsert_user(
            db=db_session,
            sub="user-2",
            email="user2@example.com",
            first_name="User",
            last_name="Two",
        )

        group = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        members = await user_group_service.add_members(
            db=db_session,
            group_id=group.id,
            actor_sub="test-actor",
            user_ids=["user-1", "user-2"],
            role="read",
        )

        assert len(members) == 2

    async def test_update_member_role(self, user_service, user_group_service: UserGroupService, db_session):
        """Test updating a member's role."""
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        group = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        await user_group_service.add_members(
            db=db_session, group_id=group.id, user_ids=["test-user"], role="read", actor_sub="test-actor"
        )

        # Update to admin
        updated = await user_group_service.update_member_role(
            db=db_session, group_id=group.id, user_id="test-user", role="manager", actor_sub="test-actor"
        )

        assert updated is not None
        assert updated.group_role == "manager"

    async def test_remove_member(self, user_service, user_group_service: UserGroupService, db_session):
        """Test removing a member from a group."""
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        group = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        await user_group_service.add_members(
            db=db_session, group_id=group.id, user_ids=["test-user"], role="read", actor_sub="test-actor"
        )

        # Remove member
        success = await user_group_service.remove_member(
            db=db_session, group_id=group.id, user_id="test-user", actor_sub="test-actor"
        )

        assert success is True

        members, total = await user_group_service.list_members(db_session, group.id)
        assert total == 0

    async def test_get_user_groups(self, user_service, user_group_service: UserGroupService, db_session):
        """Test getting all groups a user belongs to."""
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        group1 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Group 1")
        group2 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Group 2")

        await user_group_service.add_members(
            db=db_session, group_id=group1.id, user_ids=["test-user"], role="read", actor_sub="test-actor"
        )
        await user_group_service.add_members(
            db=db_session, group_id=group2.id, user_ids=["test-user"], role="manager", actor_sub="test-actor"
        )

        groups = await user_group_service.list_user_groups(db_session, "test-user")

        assert len(groups) == 2
        group_ids = [g.id for g in groups]
        assert group1.id in group_ids
        assert group2.id in group_ids

    async def test_is_group_admin(self, user_service, user_group_service: UserGroupService, db_session):
        """Test checking if user is group admin."""
        await user_service.upsert_user(
            db=db_session,
            sub="admin-user",
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
        )
        await user_service.upsert_user(
            db=db_session,
            sub="member-user",
            email="member@example.com",
            first_name="Member",
            last_name="User",
        )

        group = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        await user_group_service.add_members(
            db=db_session, group_id=group.id, user_ids=["admin-user"], role="manager", actor_sub="test-actor"
        )
        await user_group_service.add_members(
            db=db_session, group_id=group.id, user_ids=["member-user"], role="read", actor_sub="test-actor"
        )

        assert await user_group_service.is_group_admin(db_session, group.id, "admin-user") is True
        assert await user_group_service.is_group_admin(db_session, group.id, "member-user") is False
        assert await user_group_service.is_group_admin(db_session, group.id, "non-member") is False

    async def test_is_group_member(self, user_service, user_group_service: UserGroupService, db_session):
        """Test checking if user is group member."""
        await user_service.upsert_user(
            db=db_session,
            sub="test-user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        group = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Test Group")

        # Not a member initially
        assert await user_group_service.is_group_member(db_session, group.id, "test-user") is False

        # Add as member
        await user_group_service.add_members(
            db=db_session, group_id=group.id, user_ids=["test-user"], role="read", actor_sub="test-actor"
        )

        assert await user_group_service.is_group_member(db_session, group.id, "test-user") is True

    async def test_bulk_delete_groups(self, user_group_service: UserGroupService, db_session):
        """Test bulk deleting groups."""
        group1 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Group 1")
        group2 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Group 2")
        group3 = await user_group_service.create_group(db=db_session, actor_sub="test-actor", name="Group 3")

        results = await user_group_service.bulk_delete_groups(
            db=db_session, group_ids=[group1.id, group2.id], actor_sub="test-actor"
        )

        # Results is a list of BulkDeleteResult
        result_map = {r.group_id: r for r in results}
        assert result_map[group1.id].success is True
        assert result_map[group2.id].success is True

        # Verify group3 still exists
        groups, total = await user_group_service.list_groups(db_session)
        assert total == 1
        assert groups[0].id == group3.id
