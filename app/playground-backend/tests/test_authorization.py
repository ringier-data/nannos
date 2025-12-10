"""Tests for RBAC authorization system.

Tests the authorization constants and permission checking logic without
testing Pydantic models or framework behavior.
"""

import pytest
import pytest_asyncio
from sqlalchemy import text

from playground_backend.authorization import (
    GROUP_ROLE_CAPABILITIES,
    SYSTEM_ROLE_CAPABILITIES,
    check_action_allowed,
)
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


@pytest_asyncio.fixture
async def db_session(pg_session):
    """Alias for pg_session to match test expectations."""
    yield pg_session


class TestCheckActionAllowed:
    """Test check_action_allowed() function with various role/action combinations."""

    def test_read_role_sub_agents_read(self):
        """Read role should allow reading sub-agents."""
        assert check_action_allowed("read", "sub_agents", "read") is True

    def test_read_role_sub_agents_write(self):
        """Read role should not allow writing sub-agents."""
        assert check_action_allowed("read", "sub_agents", "write") is False

    def test_read_role_members_read(self):
        """Read role should allow reading members."""
        assert check_action_allowed("read", "members", "read") is True

    def test_read_role_members_write(self):
        """Read role should not allow writing members."""
        assert check_action_allowed("read", "members", "write") is False

    def test_write_role_sub_agents_read(self):
        """Write role should allow reading sub-agents."""
        assert check_action_allowed("write", "sub_agents", "read") is True

    def test_write_role_sub_agents_write(self):
        """Write role should allow writing sub-agents."""
        assert check_action_allowed("write", "sub_agents", "write") is True

    def test_write_role_members_read(self):
        """Write role should allow reading members."""
        assert check_action_allowed("write", "members", "read") is True

    def test_write_role_members_write(self):
        """Write role should not allow writing members (only managers can)."""
        assert check_action_allowed("write", "members", "write") is False

    def test_manager_role_sub_agents_read(self):
        """Manager role should allow reading sub-agents."""
        assert check_action_allowed("manager", "sub_agents", "read") is True

    def test_manager_role_sub_agents_write(self):
        """Manager role should allow writing sub-agents."""
        assert check_action_allowed("manager", "sub_agents", "write") is True

    def test_manager_role_members_read(self):
        """Manager role should allow reading members."""
        assert check_action_allowed("manager", "members", "read") is True

    def test_manager_role_members_write(self):
        """Manager role should allow writing members."""
        assert check_action_allowed("manager", "members", "write") is True

    def test_invalid_role(self):
        """Invalid role should return False."""
        assert check_action_allowed("invalid_role", "sub_agents", "read") is False

    def test_invalid_resource_type(self):
        """Invalid resource type should return False."""
        assert check_action_allowed("read", "invalid_resource", "read") is False

    def test_invalid_action(self):
        """Invalid action should return False."""
        assert check_action_allowed("read", "sub_agents", "delete") is False

    def test_empty_string_role(self):
        """Empty string role should return False."""
        assert check_action_allowed("", "sub_agents", "read") is False

    def test_empty_string_resource(self):
        """Empty string resource should return False."""
        assert check_action_allowed("read", "", "read") is False

    def test_empty_string_action(self):
        """Empty string action should return False."""
        assert check_action_allowed("read", "sub_agents", "") is False


class TestSystemRoleCapabilities:
    """Test SYSTEM_ROLE_CAPABILITIES constants."""

    def test_member_role_exists(self):
        """Member role should be defined."""
        assert "member" in SYSTEM_ROLE_CAPABILITIES

    def test_member_can_read_groups(self):
        """Members should be able to read groups."""
        assert "read" in SYSTEM_ROLE_CAPABILITIES["member"]["groups"]

    def test_member_cannot_write_groups(self):
        """Members should not have write access to groups."""
        assert "write" not in SYSTEM_ROLE_CAPABILITIES["member"]["groups"]

    def test_member_can_read_write_members(self):
        """Members should have read/write access to members (intersected with group role)."""
        assert "read" in SYSTEM_ROLE_CAPABILITIES["member"]["members"]
        assert "write" in SYSTEM_ROLE_CAPABILITIES["member"]["members"]

    def test_member_can_read_write_sub_agents(self):
        """Members should have read/write access to sub-agents (intersected with group role)."""
        assert "read" in SYSTEM_ROLE_CAPABILITIES["member"]["sub_agents"]
        assert "write" in SYSTEM_ROLE_CAPABILITIES["member"]["sub_agents"]

    def test_member_cannot_approve_sub_agents(self):
        """Members should not have approve capability."""
        assert "approve" not in SYSTEM_ROLE_CAPABILITIES["member"]["sub_agents"]

    def test_approver_role_exists(self):
        """Approver role should be defined."""
        assert "approver" in SYSTEM_ROLE_CAPABILITIES

    def test_approver_can_approve_sub_agents(self):
        """Approvers should have approve capability for sub-agents."""
        assert "approve" in SYSTEM_ROLE_CAPABILITIES["approver"]["sub_agents"]

    def test_approver_can_read_write_sub_agents(self):
        """Approvers should have read/write access to sub-agents."""
        assert "read" in SYSTEM_ROLE_CAPABILITIES["approver"]["sub_agents"]
        assert "write" in SYSTEM_ROLE_CAPABILITIES["approver"]["sub_agents"]

    def test_admin_role_exists(self):
        """Admin role should be defined."""
        assert "admin" in SYSTEM_ROLE_CAPABILITIES

    def test_admin_can_approve_sub_agents(self):
        """Admins should have approve.admin capability for sub-agents."""
        assert "approve.admin" in SYSTEM_ROLE_CAPABILITIES["admin"]["sub_agents"]

    def test_admin_can_read_write_users(self):
        """Admins should have read.admin/write.admin access to users."""
        assert "read.admin" in SYSTEM_ROLE_CAPABILITIES["admin"]["users"]
        assert "write.admin" in SYSTEM_ROLE_CAPABILITIES["admin"]["users"]

    def test_member_has_no_users_access(self):
        """Members should not have access to users resource."""
        assert "users" not in SYSTEM_ROLE_CAPABILITIES["member"]

    def test_approver_has_no_users_access(self):
        """Approvers should not have access to users resource."""
        assert "users" not in SYSTEM_ROLE_CAPABILITIES["approver"]


class TestGroupRoleCapabilities:
    """Test GROUP_ROLE_CAPABILITIES constants."""

    def test_read_role_exists(self):
        """Read role should be defined."""
        assert "read" in GROUP_ROLE_CAPABILITIES

    def test_read_role_sub_agents_read_only(self):
        """Read role should only have read access to sub-agents."""
        assert "read" in GROUP_ROLE_CAPABILITIES["read"]["sub_agents"]
        assert "write" not in GROUP_ROLE_CAPABILITIES["read"]["sub_agents"]

    def test_read_role_members_read_only(self):
        """Read role should only have read access to members."""
        assert "read" in GROUP_ROLE_CAPABILITIES["read"]["members"]
        assert "write" not in GROUP_ROLE_CAPABILITIES["read"]["members"]

    def test_write_role_exists(self):
        """Write role should be defined."""
        assert "write" in GROUP_ROLE_CAPABILITIES

    def test_write_role_sub_agents_read_write(self):
        """Write role should have read/write access to sub-agents."""
        assert "read" in GROUP_ROLE_CAPABILITIES["write"]["sub_agents"]
        assert "write" in GROUP_ROLE_CAPABILITIES["write"]["sub_agents"]

    def test_write_role_members_read_only(self):
        """Write role should only have read access to members."""
        assert "read" in GROUP_ROLE_CAPABILITIES["write"]["members"]
        assert "write" not in GROUP_ROLE_CAPABILITIES["write"]["members"]

    def test_manager_role_exists(self):
        """Manager role should be defined."""
        assert "manager" in GROUP_ROLE_CAPABILITIES

    def test_manager_role_sub_agents_read_write(self):
        """Manager role should have read/write access to sub-agents."""
        assert "read" in GROUP_ROLE_CAPABILITIES["manager"]["sub_agents"]
        assert "write" in GROUP_ROLE_CAPABILITIES["manager"]["sub_agents"]

    def test_manager_role_members_read_write(self):
        """Manager role should have read/write access to members."""
        assert "read" in GROUP_ROLE_CAPABILITIES["manager"]["members"]
        assert "write" in GROUP_ROLE_CAPABILITIES["manager"]["members"]


@pytest.mark.asyncio
class TestCheckUserPermission:
    """Test check_user_permission() method for system-level authorization."""

    async def _create_user_with_role(self, db_session, role: str, sub: str | None = None):
        """Helper to create a user with specific role."""
        if sub is None:
            sub = f"user-{role}"

        # Insert user directly with specific role
        query = text("""
            INSERT INTO users (id, sub, email, role, status, first_name, last_name)
            VALUES (:id, :sub, :email, :role, 'active', 'Test', 'User')
            RETURNING id
        """)
        result = await db_session.execute(query, {"id": sub, "sub": sub, "email": f"{sub}@example.com", "role": role})
        await db_session.commit()
        row = result.first()
        return row[0]

    async def test_member_can_read_groups(self, user_group_service, db_session):
        """Members should be able to read groups."""
        user_id = await self._create_user_with_role(db_session, "member", "member-1")

        has_permission = await user_group_service.check_user_permission(db_session, user_id, "groups", "read")

        assert has_permission is True

    async def test_member_cannot_write_groups(self, user_group_service, db_session):
        """Members should not be able to write groups."""
        user_id = await self._create_user_with_role(db_session, "member", "member-2")

        has_permission = await user_group_service.check_user_permission(db_session, user_id, "groups", "write")

        assert has_permission is False

    async def test_member_can_read_write_sub_agents(self, user_group_service, db_session):
        """Members should have read/write capability for sub-agents (intersected with group role)."""
        user_id = await self._create_user_with_role(db_session, "member", "member-3")

        can_read = await user_group_service.check_user_permission(db_session, user_id, "sub_agents", "read")
        can_write = await user_group_service.check_user_permission(db_session, user_id, "sub_agents", "write")

        assert can_read is True
        assert can_write is True

    async def test_member_cannot_approve_sub_agents(self, user_group_service, db_session):
        """Members should not have approve capability."""
        user_id = await self._create_user_with_role(db_session, "member", "member-4")

        has_permission = await user_group_service.check_user_permission(db_session, user_id, "sub_agents", "approve")

        assert has_permission is False

    async def test_approver_can_approve_sub_agents(self, user_group_service, db_session):
        """Approvers should have approve capability."""
        user_id = await self._create_user_with_role(db_session, "approver", "approver-1")

        has_permission = await user_group_service.check_user_permission(db_session, user_id, "sub_agents", "approve")

        assert has_permission is True

    async def test_admin_can_approve_sub_agents(self, user_group_service, db_session):
        """Admins should have approve.admin capability."""
        user_id = await self._create_user_with_role(db_session, "admin", "admin-1")

        has_permission = await user_group_service.check_user_permission(
            db_session, user_id, "sub_agents", "approve.admin"
        )

        assert has_permission is True

    async def test_admin_can_read_write_users(self, user_group_service, db_session):
        """Admins should have read.admin/write.admin access to users."""
        user_id = await self._create_user_with_role(db_session, "admin", "admin-2")

        can_read = await user_group_service.check_user_permission(db_session, user_id, "users", "read.admin")
        can_write = await user_group_service.check_user_permission(db_session, user_id, "users", "write.admin")

        assert can_read is True
        assert can_write is True

    async def test_member_cannot_access_users(self, user_group_service, db_session):
        """Members should not have access to users resource."""
        user_id = await self._create_user_with_role(db_session, "member", "member-5")

        can_read = await user_group_service.check_user_permission(db_session, user_id, "users", "read")
        can_write = await user_group_service.check_user_permission(db_session, user_id, "users", "write")

        assert can_read is False
        assert can_write is False

    async def test_approver_cannot_access_users(self, user_group_service, db_session):
        """Approvers should not have access to users resource."""
        user_id = await self._create_user_with_role(db_session, "approver", "approver-2")

        can_read = await user_group_service.check_user_permission(db_session, user_id, "users", "read")
        can_write = await user_group_service.check_user_permission(db_session, user_id, "users", "write")

        assert can_read is False
        assert can_write is False

    async def test_invalid_user_id(self, user_group_service, db_session):
        """Non-existent user should return False."""
        has_permission = await user_group_service.check_user_permission(
            db_session, "nonexistent-user", "groups", "read"
        )

        assert has_permission is False

    async def test_invalid_resource(self, user_group_service, db_session):
        """Invalid resource should return False."""
        user_id = await self._create_user_with_role(db_session, "member", "member-6")

        has_permission = await user_group_service.check_user_permission(db_session, user_id, "invalid_resource", "read")

        assert has_permission is False

    async def test_invalid_action(self, user_group_service, db_session):
        """Invalid action should return False."""
        user_id = await self._create_user_with_role(db_session, "member", "member-7")

        has_permission = await user_group_service.check_user_permission(db_session, user_id, "groups", "delete")

        assert has_permission is False


@pytest.mark.asyncio
class TestCheckResourcePermission:
    """Test check_resource_permission() method for resource-level authorization."""

    async def _create_user_with_role(self, db_session, role: str, sub: str | None = None):
        """Helper to create a user with specific role."""
        if sub is None:
            sub = f"user-{role}"

        query = text("""
            INSERT INTO users (id, sub, email, role, status, first_name, last_name)
            VALUES (:id, :sub, :email, :role, 'active', 'Test', 'User')
            RETURNING id
        """)
        result = await db_session.execute(query, {"id": sub, "sub": sub, "email": f"{sub}@example.com", "role": role})
        await db_session.commit()
        row = result.first()
        return row[0]

    async def _create_group_with_member(self, user_group_service, db_session, user_id: str, group_role: str):
        """Helper to create a group and add user as member."""
        group = await user_group_service.create_group(db_session, name=f"Test Group {user_id}")

        # Add user to group with specific role
        query = text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (:group_id, :user_id, :group_role)
        """)
        await db_session.execute(query, {"group_id": group.id, "user_id": user_id, "group_role": group_role})
        await db_session.commit()

        return group

    async def _create_sub_agent(self, db_session, owner_id: str, is_public: bool = False):
        """Helper to create a sub-agent."""
        # Create sub_agent (metadata only)
        query = text("""
            INSERT INTO sub_agents (name, type, owner_user_id, is_public)
            VALUES (:name, 'remote', :owner_id, :is_public)
            RETURNING id
        """)
        result = await db_session.execute(
            query,
            {
                "name": f"Test Agent {owner_id}",
                "owner_id": owner_id,
                "is_public": is_public,
            },
        )
        sub_agent_id = result.scalar()

        # Create initial version (configuration)
        version_query = text("""
            INSERT INTO sub_agent_config_versions 
            (sub_agent_id, version, description, agent_url, status)
            VALUES (:sub_agent_id, 1, 'Test sub-agent', 'https://example.com', 'approved')
        """)
        await db_session.execute(
            version_query,
            {"sub_agent_id": sub_agent_id},
        )
        await db_session.commit()
        return sub_agent_id

    async def _grant_sub_agent_permission(self, db_session, group_id: int, sub_agent_id: int, permissions: list):
        """Helper to grant sub-agent permissions to a group."""
        query = text("""
            INSERT INTO sub_agent_permissions (user_group_id, sub_agent_id, permissions)
            VALUES (:group_id, :sub_agent_id, :permissions)
        """)
        await db_session.execute(
            query, {"group_id": group_id, "sub_agent_id": sub_agent_id, "permissions": permissions}
        )
        await db_session.commit()

    async def test_owner_has_full_access(self, user_group_service, db_session):
        """Resource owners should have full access regardless of group membership."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-1")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        can_read = await user_group_service.check_resource_permission(
            db_session, owner_id, "sub_agents", sub_agent_id, "read"
        )
        can_write = await user_group_service.check_resource_permission(
            db_session, owner_id, "sub_agents", sub_agent_id, "write"
        )

        assert can_read is True
        assert can_write is True

    async def test_public_sub_agent_read_access(self, user_group_service, db_session):
        """Public sub-agents should allow read access to all users."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-2")
        other_user_id = await self._create_user_with_role(db_session, "member", "other-1")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id, is_public=True)

        can_read = await user_group_service.check_resource_permission(
            db_session, other_user_id, "sub_agents", sub_agent_id, "read"
        )

        assert can_read is True

    async def test_public_sub_agent_no_write_access(self, user_group_service, db_session):
        """Public sub-agents should not allow write access without group membership."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-3")
        other_user_id = await self._create_user_with_role(db_session, "member", "other-2")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id, is_public=True)

        can_write = await user_group_service.check_resource_permission(
            db_session, other_user_id, "sub_agents", sub_agent_id, "write"
        )

        assert can_write is False

    async def test_read_role_can_read_with_permission(self, user_group_service, db_session):
        """User with 'read' group role can read when group has read permission."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-4")
        user_id = await self._create_user_with_role(db_session, "member", "reader-1")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, user_id, "read")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read", "write"])

        can_read = await user_group_service.check_resource_permission(
            db_session, user_id, "sub_agents", sub_agent_id, "read"
        )

        assert can_read is True

    async def test_read_role_cannot_write(self, user_group_service, db_session):
        """User with 'read' group role cannot write even when group has write permission."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-5")
        user_id = await self._create_user_with_role(db_session, "member", "reader-2")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, user_id, "read")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read", "write"])

        can_write = await user_group_service.check_resource_permission(
            db_session, user_id, "sub_agents", sub_agent_id, "write"
        )

        assert can_write is False

    async def test_write_role_can_read_write(self, user_group_service, db_session):
        """User with 'write' group role can read and write when group has permissions."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-6")
        user_id = await self._create_user_with_role(db_session, "member", "writer-1")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, user_id, "write")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read", "write"])

        can_read = await user_group_service.check_resource_permission(
            db_session, user_id, "sub_agents", sub_agent_id, "read"
        )
        can_write = await user_group_service.check_resource_permission(
            db_session, user_id, "sub_agents", sub_agent_id, "write"
        )

        assert can_read is True
        assert can_write is True

    async def test_write_role_cannot_write_without_group_permission(self, user_group_service, db_session):
        """User with 'write' role cannot write if group only has 'read' permission."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-7")
        user_id = await self._create_user_with_role(db_session, "member", "writer-2")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, user_id, "write")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read"])

        can_write = await user_group_service.check_resource_permission(
            db_session, user_id, "sub_agents", sub_agent_id, "write"
        )

        assert can_write is False

    async def test_approver_can_approve_with_group_access(self, user_group_service, db_session):
        """Approver can approve if they have write group role and group has write permission."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-8")
        approver_id = await self._create_user_with_role(db_session, "approver", "approver-1")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, approver_id, "write")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read", "write"])

        can_approve = await user_group_service.check_resource_permission(
            db_session, approver_id, "sub_agents", sub_agent_id, "approve"
        )

        assert can_approve is True

    async def test_approver_cannot_approve_without_group_write_permission(self, user_group_service, db_session):
        """Approver cannot approve if group only has read permission."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-9")
        approver_id = await self._create_user_with_role(db_session, "approver", "approver-2")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, approver_id, "write")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read"])

        can_approve = await user_group_service.check_resource_permission(
            db_session, approver_id, "sub_agents", sub_agent_id, "approve"
        )

        assert can_approve is False

    async def test_approver_cannot_approve_with_read_role(self, user_group_service, db_session):
        """Approver with 'read' group role cannot approve (needs write or manager)."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-10")
        approver_id = await self._create_user_with_role(db_session, "approver", "approver-3")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, approver_id, "read")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read", "write"])

        can_approve = await user_group_service.check_resource_permission(
            db_session, approver_id, "sub_agents", sub_agent_id, "approve"
        )

        assert can_approve is False

    async def test_member_cannot_approve_even_with_manager_role(self, user_group_service, db_session):
        """Member (system role) cannot approve even with 'manager' group role."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-11")
        member_id = await self._create_user_with_role(db_session, "member", "member-mgr-1")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, member_id, "manager")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read", "write"])

        can_approve = await user_group_service.check_resource_permission(
            db_session, member_id, "sub_agents", sub_agent_id, "approve"
        )

        assert can_approve is False

    async def test_no_access_without_group_membership(self, user_group_service, db_session):
        """User without group membership should not have access."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-12")
        other_user_id = await self._create_user_with_role(db_session, "member", "other-3")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        can_read = await user_group_service.check_resource_permission(
            db_session, other_user_id, "sub_agents", sub_agent_id, "read"
        )
        can_write = await user_group_service.check_resource_permission(
            db_session, other_user_id, "sub_agents", sub_agent_id, "write"
        )

        assert can_read is False
        assert can_write is False

    async def test_manager_role_full_access(self, user_group_service, db_session):
        """Manager group role should have full read/write access."""
        owner_id = await self._create_user_with_role(db_session, "member", "owner-13")
        manager_id = await self._create_user_with_role(db_session, "member", "manager-1")
        sub_agent_id = await self._create_sub_agent(db_session, owner_id)

        group = await self._create_group_with_member(user_group_service, db_session, manager_id, "manager")
        await self._grant_sub_agent_permission(db_session, group.id, sub_agent_id, ["read", "write"])

        can_read = await user_group_service.check_resource_permission(
            db_session, manager_id, "sub_agents", sub_agent_id, "read"
        )
        can_write = await user_group_service.check_resource_permission(
            db_session, manager_id, "sub_agents", sub_agent_id, "write"
        )

        assert can_read is True
        assert can_write is True
