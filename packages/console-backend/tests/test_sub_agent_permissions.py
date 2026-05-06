"""Tests for SubAgentService with group permissions.

These tests use the PostgreSQL fixtures with Rambler migrations to ensure
schema parity with production.
"""

import pytest
import pytest_asyncio
from console_backend.models.sub_agent import SubAgentCreate, SubAgentType
from console_backend.models.user import User, UserStatus
from console_backend.services.sub_agent_service import SubAgentService
from console_backend.services.user_service import UserService
from sqlalchemy import text


# Alias pg_session to db_session for compatibility with tests
@pytest_asyncio.fixture
async def db_session(pg_session):
    """Alias for pg_session to match test expectations."""
    yield pg_session


@pytest.mark.asyncio
class TestSubAgentServiceGroupPermissions:
    """Test SubAgentService group permission functionality."""

    async def _create_test_sub_agent(
        self,
        sub_agent_service: SubAgentService,
        db_session,
        actor: User,
        name: str = "Test Agent",
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
            actor=actor,
        )

    async def test_owner_status_sync_trigger(
        self,
        sub_agent_service: SubAgentService,
        user_service: UserService,
        db_session,
        test_admin_user_db: User,
        test_user_db: User,
    ):
        """Test that owner_status is synced when user status changes."""

        # Create sub-agent
        agent = await self._create_test_sub_agent(sub_agent_service, db_session, actor=test_user_db, name="Test Agent")

        # Verify initial owner_status is 'active'
        result = await db_session.execute(
            text("SELECT owner_status FROM sub_agents WHERE id = :id"),
            {"id": agent.id},
        )
        row = result.fetchone()
        assert row[0] == "active"

        # Suspend the user
        await user_service.update_user_status(
            db=db_session, actor=test_admin_user_db, user_id=test_user_db.id, status=UserStatus.SUSPENDED
        )

        # Verify owner_status was synced to 'suspended'
        result = await db_session.execute(
            text("SELECT owner_status FROM sub_agents WHERE id = :id"),
            {"id": agent.id},
        )
        row = result.fetchone()
        assert row[0] == "suspended"
