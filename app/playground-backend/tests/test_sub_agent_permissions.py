"""Tests for SubAgentService with group permissions.

These tests use the PostgreSQL fixtures with Rambler migrations to ensure
schema parity with production.
"""

import pytest
import pytest_asyncio
from sqlalchemy import text

from playground_backend.models.sub_agent import SubAgentCreate, SubAgentStatus, SubAgentType
from playground_backend.models.user import UserStatus


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
        await user_service.update_user_status(
            db=db_session, user_id="owner", actor_sub="admin", status=UserStatus.SUSPENDED
        )

        # Verify owner_status was synced to 'suspended'
        result = await db_session.execute(
            text("SELECT owner_status FROM sub_agents WHERE id = :id"),
            {"id": agent.id},
        )
        row = result.fetchone()
        assert row[0] == "suspended"
