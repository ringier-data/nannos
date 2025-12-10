"""Tests for SubAgentService with group permissions.

These tests use the PostgreSQL fixtures with Rambler migrations to ensure
schema parity with production.
"""

import pytest
import pytest_asyncio
from sqlalchemy import text

from playground_backend.models.sub_agent import SubAgentCreate, SubAgentStatus, SubAgentType
from playground_backend.models.user import UserStatus
from playground_backend.services.sub_agent_service import SubAgentService
from playground_backend.services.user_group_service import UserGroupService
from playground_backend.services.user_service import UserService

# Import the postgres fixtures


@pytest.fixture
def sub_agent_service():
    """Create SubAgentService instance."""
    return SubAgentService()


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
class TestSubAgentServiceGroupPermissions:
    """Test SubAgentService group permission functionality."""

    async def _create_test_user(self, user_service, db_session, sub: str = "test-user"):
        """Helper to create a test user."""
        return await user_service.upsert_user(
            db=db_session,
            sub=sub,
            email=f"{sub}@example.com",
            first_name="Test",
            last_name="User",
        )

    async def _create_test_sub_agent(
        self,
        sub_agent_service,
        db_session,
        owner_id: str,
        name: str = "Test Agent",
        status: SubAgentStatus = SubAgentStatus.APPROVED,
    ):
        """Helper to create a test sub-agent."""

        create_data = SubAgentCreate(
            name=name,
            description="Test description",
            type=SubAgentType.REMOTE,
            agent_url="https://example.com/agent",
        )

        return await sub_agent_service.create_sub_agent(
            db=db_session,
            data=create_data,
            user_id=owner_id,
        )

    async def test_list_by_group_empty(self, sub_agent_service, user_group_service, db_session):
        """Test listing sub-agents for a group with no permissions."""
        group = await user_group_service.create_group(db=db_session, name="Empty Group", permissions={})

        sub_agents, total = await sub_agent_service.list_by_group(db=db_session, group_id=group.id)

        assert sub_agents == []
        assert total == 0

    async def test_list_by_group_with_permissions(
        self,
        sub_agent_service,
        user_service,
        user_group_service,
        db_session,
    ):
        """Test listing sub-agents accessible by a group."""
        # Create user and sub-agent
        await self._create_test_user(user_service, db_session, "owner")
        agent = await self._create_test_sub_agent(sub_agent_service, db_session, "owner", "My Agent")

        # Create group
        group = await user_group_service.create_group(db=db_session, name="Test Group", permissions={})

        # Grant group access to sub-agent (direct SQL since we don't have a service method)

        await db_session.execute(
            text("""
                INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id)
                VALUES (:sub_agent_id, :group_id)
            """),
            {"sub_agent_id": agent.id, "group_id": group.id},
        )
        await db_session.commit()

        # List sub-agents for group
        sub_agents, total = await sub_agent_service.list_by_group(db=db_session, group_id=group.id)

        assert total == 1
        assert sub_agents[0].id == agent.id
        assert sub_agents[0].name == "My Agent"

    async def test_list_by_group_pagination(
        self,
        sub_agent_service,
        user_service,
        user_group_service,
        db_session,
    ):
        """Test pagination when listing sub-agents by group."""

        # Create user
        await self._create_test_user(user_service, db_session, "owner")

        # Create group
        group = await user_group_service.create_group(db=db_session, name="Test Group", permissions={})

        # Create multiple sub-agents and grant access
        for i in range(5):
            agent = await self._create_test_sub_agent(sub_agent_service, db_session, "owner", f"Agent {i}")
            await db_session.execute(
                text("""
                    INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id)
                    VALUES (:sub_agent_id, :group_id)
                """),
                {"sub_agent_id": agent.id, "group_id": group.id},
            )
        await db_session.commit()

        # Get first page
        sub_agents, total = await sub_agent_service.list_by_group(db=db_session, group_id=group.id, page=1, limit=2)

        assert len(sub_agents) == 2
        assert total == 5

        # Get second page
        sub_agents, total = await sub_agent_service.list_by_group(db=db_session, group_id=group.id, page=2, limit=2)

        assert len(sub_agents) == 2
        assert total == 5

    async def test_list_by_group_excludes_deleted(
        self,
        sub_agent_service,
        user_service,
        user_group_service,
        db_session,
    ):
        """Test that deleted sub-agents are excluded."""
        # Create user and sub-agents
        await self._create_test_user(user_service, db_session, "owner")
        active_agent = await self._create_test_sub_agent(sub_agent_service, db_session, "owner", "Active Agent")
        deleted_agent = await self._create_test_sub_agent(sub_agent_service, db_session, "owner", "Deleted Agent")

        # Create group
        group = await user_group_service.create_group(db=db_session, name="Test Group", permissions={})

        # Grant access to both
        await db_session.execute(
            text("""
                INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id)
                VALUES (:active_id, :group_id), (:deleted_id, :group_id)
            """),
            {
                "active_id": active_agent.id,
                "deleted_id": deleted_agent.id,
                "group_id": group.id,
            },
        )

        # Soft delete one agent
        await db_session.execute(
            text("""
                UPDATE sub_agents SET deleted_at = NOW() WHERE id = :id
            """),
            {"id": deleted_agent.id},
        )
        await db_session.commit()

        # List should only return active agent
        sub_agents, total = await sub_agent_service.list_by_group(db=db_session, group_id=group.id)

        assert total == 1
        assert sub_agents[0].id == active_agent.id

    async def test_list_by_group_filter_by_status(
        self,
        sub_agent_service,
        user_service,
        user_group_service,
        db_session,
    ):
        """Test filtering sub-agents by status."""

        # Create user
        await self._create_test_user(user_service, db_session, "owner")

        # Create agents with different statuses
        approved = await self._create_test_sub_agent(sub_agent_service, db_session, "owner", "Approved Agent")
        draft = await self._create_test_sub_agent(sub_agent_service, db_session, "owner", "Draft Agent")

        # Update statuses
        await db_session.execute(
            text("UPDATE sub_agents SET status = 'approved' WHERE id = :id"),
            {"id": approved.id},
        )
        await db_session.execute(
            text("UPDATE sub_agents SET status = 'draft' WHERE id = :id"),
            {"id": draft.id},
        )

        # Create group
        group = await user_group_service.create_group(db=db_session, name="Test Group", permissions={})

        # Grant access to both
        await db_session.execute(
            text("""
                INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id)
                VALUES (:approved_id, :group_id), (:draft_id, :group_id)
            """),
            {
                "approved_id": approved.id,
                "draft_id": draft.id,
                "group_id": group.id,
            },
        )
        await db_session.commit()

        # Filter by approved status
        sub_agents, total = await sub_agent_service.list_by_group(
            db=db_session, group_id=group.id, status=SubAgentStatus.APPROVED
        )

        assert total == 1
        assert sub_agents[0].id == approved.id

    async def test_owner_status_sync_trigger(
        self,
        sub_agent_service,
        user_service,
        db_session,
    ):
        """Test that owner_status is synced when user status changes."""

        # Create user
        await self._create_test_user(user_service, db_session, "owner")

        # Create sub-agent
        agent = await self._create_test_sub_agent(sub_agent_service, db_session, "owner", "Test Agent")

        # Verify initial owner_status is 'active'
        result = await db_session.execute(
            text("SELECT owner_status FROM sub_agents WHERE id = :id"),
            {"id": agent.id},
        )
        row = result.fetchone()
        assert row[0] == "active"

        # Suspend the user
        await user_service.update_user_status(db=db_session, user_id="owner", status=UserStatus.SUSPENDED)

        # Verify owner_status was synced to 'suspended'
        result = await db_session.execute(
            text("SELECT owner_status FROM sub_agents WHERE id = :id"),
            {"id": agent.id},
        )
        row = result.fetchone()
        assert row[0] == "suspended"
