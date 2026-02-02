"""Tests for Audit Log functionality.

These tests use the PostgreSQL fixtures with Rambler migrations to ensure
schema parity with production.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.audit import AuditAction, AuditEntityType
from playground_backend.services.audit_service import AuditService


@pytest.fixture
def audit_service():
    """Create AuditService instance."""
    return AuditService()


# Alias pg_session to db_session for compatibility with tests
@pytest_asyncio.fixture
async def db_session(pg_session):
    """Alias for pg_session to match test expectations."""
    yield pg_session


@pytest.mark.asyncio
class TestAuditService:
    """Test AuditService functionality."""

    async def test_log_action_user_create(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test logging a user creation action."""
        log = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="new-user-id",
            action=AuditAction.CREATE,
            changes={
                "email": "newuser@example.com",
                "first_name": "New",
                "last_name": "User",
            },
        )

        assert log is not None
        assert log.actor_sub == test_user.sub
        assert log.entity_type == AuditEntityType.USER
        assert log.entity_id == "new-user-id"
        assert log.action == AuditAction.CREATE
        assert log.changes["email"] == "newuser@example.com"
        assert log.created_at is not None

    async def test_log_action_user_update(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test logging a user update action."""
        log = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="existing-user",
            action=AuditAction.UPDATE,
            changes={
                "status": {"old": "active", "new": "suspended"},
            },
        )

        assert log.action == AuditAction.UPDATE
        assert log.changes["status"]["old"] == "active"
        assert log.changes["status"]["new"] == "suspended"

    async def test_log_action_group(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test logging a group-related action."""
        log = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.GROUP,
            entity_id="1",
            action=AuditAction.CREATE,
            changes={
                "name": "New Group",
                "permissions": {"sub_agents": ["read"]},
            },
        )

        assert log.entity_type == AuditEntityType.GROUP
        assert log.entity_id == "1"

    async def test_log_action_sub_agent(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test logging a sub-agent action."""
        log = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.SUB_AGENT,
            entity_id="42",
            action=AuditAction.APPROVE,
            changes={
                "status": {"old": "pending_approval", "new": "approved"},
                "approved_by": "admin-user",
            },
        )

        assert log.entity_type == AuditEntityType.SUB_AGENT
        assert log.action == AuditAction.APPROVE

    async def test_list_logs_empty(self, audit_service: AuditService, db_session: AsyncSession):
        """Test listing logs when none exist."""
        logs, total = await audit_service.list_logs(db_session)

        assert logs == []
        assert total == 0

    async def test_list_logs_with_data(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test listing logs with pagination."""
        # Create multiple log entries
        for i in range(5):
            await audit_service.log_action(
                db=db_session,
                actor=test_user,
                entity_type=AuditEntityType.USER,
                entity_id=f"user-{i}",
                action=AuditAction.CREATE,
                changes={"index": i},
            )

        # Get first page
        logs, total = await audit_service.list_logs(db_session, page=1, limit=2)

        assert len(logs) == 2
        assert total == 5

        # Get second page
        logs, total = await audit_service.list_logs(db_session, page=2, limit=2)

        assert len(logs) == 2
        assert total == 5

    async def test_list_logs_filter_by_entity_type(
        self, audit_service: AuditService, db_session: AsyncSession, test_user
    ):
        """Test filtering logs by entity type."""
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.CREATE,
            changes={},
        )
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.GROUP,
            entity_id="group-1",
            action=AuditAction.CREATE,
            changes={},
        )

        logs, total = await audit_service.list_logs(db_session, entity_type=AuditEntityType.USER)

        assert total == 1
        assert logs[0].entity_type == AuditEntityType.USER

    async def test_list_logs_filter_by_entity_id(
        self, audit_service: AuditService, db_session: AsyncSession, test_user
    ):
        """Test filtering logs by entity ID."""
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="specific-user",
            action=AuditAction.CREATE,
            changes={},
        )
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="other-user",
            action=AuditAction.CREATE,
            changes={},
        )

        logs, total = await audit_service.list_logs(db_session, entity_id="specific-user")

        assert total == 1
        assert logs[0].entity_id == "specific-user"

    async def test_list_logs_filter_by_action(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test filtering logs by action."""
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.CREATE,
            changes={},
        )
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.UPDATE,
            changes={},
        )

        logs, total = await audit_service.list_logs(db_session, action=AuditAction.CREATE)

        assert total == 1
        assert logs[0].action == AuditAction.CREATE

    async def test_list_logs_filter_by_actor(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test filtering logs by actor."""
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.CREATE,
            changes={},
        )
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-2",
            action=AuditAction.CREATE,
            changes={},
        )

        logs, total = await audit_service.list_logs(db_session, actor_sub=test_user.sub)

        assert total == 2
        assert logs[0].actor_sub == test_user.sub

    async def test_list_logs_combined_filters(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test filtering logs with multiple filters."""
        # Create various log entries
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.CREATE,
            changes={},
        )
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.UPDATE,
            changes={},
        )
        await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.UPDATE,
            changes={},
        )

        logs, total = await audit_service.list_logs(
            db_session,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.UPDATE,
            actor_sub=test_user.sub,
        )

        assert total == 2
        assert logs[0].actor_sub == test_user.sub
        assert logs[0].action == AuditAction.UPDATE

    async def test_list_logs_ordered_by_created_at_desc(
        self, audit_service: AuditService, db_session: AsyncSession, test_user
    ):
        """Test that logs are returned newest first.

        Since inserts happen very quickly and may have the same timestamp,
        we verify by ID order (higher ID = newer).
        """
        log1 = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.CREATE,
            changes={"order": 1},
        )
        log2 = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.UPDATE,
            changes={"order": 2},
        )
        log3 = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.USER,
            entity_id="user-1",
            action=AuditAction.DELETE,
            changes={"order": 3},
        )

        logs, _ = await audit_service.list_logs(db_session)

        # Verify we have 3 logs
        assert len(logs) == 3

        # Newest should be first - ordered by created_at DESC
        # Since timestamps may be identical, verify by ID order (higher ID = newer)
        assert logs[0].id == log3.id  # Most recent
        assert logs[1].id == log2.id
        assert logs[2].id == log1.id  # Oldest

    async def test_log_assign_action(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test logging an assign action (e.g., adding user to group)."""
        log = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.GROUP,
            entity_id="1",
            action=AuditAction.ASSIGN,
            changes={
                "user_id": "new-member",
                "role": "member",
            },
        )

        assert log.action == AuditAction.ASSIGN
        assert log.changes["user_id"] == "new-member"

    async def test_log_unassign_action(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test logging an unassign action (e.g., removing user from group)."""
        log = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.GROUP,
            entity_id="1",
            action=AuditAction.UNASSIGN,
            changes={
                "user_id": "removed-member",
            },
        )

        assert log.action == AuditAction.UNASSIGN

    async def test_log_reject_action(self, audit_service: AuditService, db_session: AsyncSession, test_user):
        """Test logging a reject action."""
        log = await audit_service.log_action(
            db=db_session,
            actor=test_user,
            entity_type=AuditEntityType.SUB_AGENT,
            entity_id="42",
            action=AuditAction.REJECT,
            changes={
                "status": {"old": "pending_approval", "new": "rejected"},
                "rejection_reason": "Does not meet security requirements",
            },
        )

        assert log.action == AuditAction.REJECT
        assert "rejection_reason" in log.changes
