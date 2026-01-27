"""Unit tests for group default agent management.

Tests cover:
- add_group_default_agent() - adding a single default agent
- remove_group_default_agent() - removing a single default agent
- set_group_default_agents() - optimized bulk update with added/removed tracking
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.notification import NotificationType


@pytest.mark.asyncio
async def test_add_group_default_agent(pg_session: AsyncSession):
    """Test adding a single default agent to a group activates it for all members."""
    from playground_backend.repositories.sub_agent_repository import SubAgentRepository
    from playground_backend.repositories.user_group_repository import UserGroupRepository
    from playground_backend.services.audit_service import AuditService
    from playground_backend.services.notification_service import NotificationService
    from playground_backend.services.sub_agent_service import SubAgentService
    from playground_backend.services.user_group_service import UserGroupService

    # Setup repositories and services
    audit_service = AuditService()
    user_group_repo = UserGroupRepository()
    sub_agent_repo = SubAgentRepository()
    sub_agent_repo.set_audit_service(audit_service)
    notification_service = NotificationService()
    sub_agent_service = SubAgentService(sub_agent_repository=sub_agent_repo, notification_service=notification_service)
    user_group_service = UserGroupService(
        user_group_repository=user_group_repo,
        sub_agent_service=sub_agent_service,
        notification_service=notification_service,
    )

    # Create test users
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status)
            VALUES ('user-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active'),
                   ('user-2', 'sub-2', 'user2@test.com', 'User', 'Two', 'member', 'active')
        """)
    )

    # Create test group
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )

    # Add members to group
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, 'user-1', 'manager'),
                   (1, 'user-2', 'read')
        """)
    )

    # Create sub-agent
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Test Agent', 'user-1', 'remote', 1)
        """)
    )

    # Add approved config version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (
                sub_agent_id, version, description, agent_url, status, approved_at, release_number
            )
            VALUES (100, 1, 'Test Version', 'https://example.com', 'approved', NOW(), 1)
        """)
    )

    # Add permission for group to access agent
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions)
            VALUES (100, 1, ARRAY['read', 'write'])
        """)
    )

    await pg_session.commit()

    # Add as default agent
    await user_group_service.add_group_default_agent(
        db=pg_session,
        group_id=1,
        sub_agent_id=100,
        actor_sub="user-1",
    )
    await pg_session.commit()

    # Verify it's a default
    result = await pg_session.execute(
        text("""
            SELECT sub_agent_id FROM user_group_default_agents
            WHERE user_group_id = 1 AND sub_agent_id = 100
        """)
    )
    assert result.first() is not None

    # Verify both users have activations
    activations_result = await pg_session.execute(
        text("""
            SELECT user_id, sub_agent_id, activated_by
            FROM user_sub_agent_activations
            WHERE sub_agent_id = 100
            ORDER BY user_id
        """)
    )
    activations = activations_result.fetchall()
    assert len(activations) == 2
    assert activations[0][0] == "user-1"
    assert activations[0][2] == "group"
    assert activations[1][0] == "user-2"
    assert activations[1][2] == "group"

    # Verify notifications were sent
    notifications_result = await pg_session.execute(
        text("""
            SELECT user_id, type, title FROM user_notifications
            WHERE type = :type
            ORDER BY user_id
        """),
        {"type": NotificationType.AGENT_ACTIVATED.value},
    )
    notifications = notifications_result.fetchall()
    assert len(notifications) == 2
    assert "Test Agent" in notifications[0][2]


@pytest.mark.asyncio
async def test_remove_group_default_agent(pg_session: AsyncSession):
    """Test removing a single default agent deactivates it for all members."""
    from playground_backend.repositories.sub_agent_repository import SubAgentRepository
    from playground_backend.repositories.user_group_repository import UserGroupRepository
    from playground_backend.services.audit_service import AuditService
    from playground_backend.services.notification_service import NotificationService
    from playground_backend.services.sub_agent_service import SubAgentService
    from playground_backend.services.user_group_service import UserGroupService

    # Setup repositories and services
    audit_service = AuditService()
    user_group_repo = UserGroupRepository()
    sub_agent_repo = SubAgentRepository()
    sub_agent_repo.set_audit_service(audit_service)
    notification_service = NotificationService()
    sub_agent_service = SubAgentService(sub_agent_repository=sub_agent_repo, notification_service=notification_service)
    user_group_service = UserGroupService(
        user_group_repository=user_group_repo,
        sub_agent_service=sub_agent_service,
        notification_service=notification_service,
    )

    # Create test users and group
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status)
            VALUES ('user-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active'),
                   ('user-2', 'sub-2', 'user2@test.com', 'User', 'Two', 'member', 'active')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, 'user-1', 'manager'),
                   (1, 'user-2', 'read')
        """)
    )

    # Create sub-agent and make it a default
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Test Agent', 'user-1', 'remote', 1)
        """)
    )

    # Add approved config version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (
                sub_agent_id, version, description, agent_url, status, approved_at, release_number
            )
            VALUES (100, 1, 'Test Version', 'https://example.com', 'approved', NOW(), 1)
        """)
    )

    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 100, 'user-1')
        """)
    )

    # Create activations for both users (use integer array not string array)
    await pg_session.execute(
        text("""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_by, activated_by_groups)
            VALUES ('user-1', 100, 'group', '[1]'::jsonb),
                   ('user-2', 100, 'group', '[1]'::jsonb)
        """)
    )
    await pg_session.commit()

    # Remove default agent
    await user_group_service.remove_group_default_agent(
        db=pg_session,
        group_id=1,
        sub_agent_id=100,
        actor_sub="user-1",
    )
    await pg_session.commit()

    # Verify it's no longer a default
    result = await pg_session.execute(
        text("""
            SELECT sub_agent_id FROM user_group_default_agents
            WHERE user_group_id = 1 AND sub_agent_id = 100
        """)
    )
    assert result.first() is None

    # Verify both users' activations were removed
    activations_result = await pg_session.execute(
        text("""
            SELECT user_id FROM user_sub_agent_activations
            WHERE sub_agent_id = 100
        """)
    )
    assert len(activations_result.fetchall()) == 0

    # Verify deactivation notifications were sent
    notifications_result = await pg_session.execute(
        text("""
            SELECT user_id, type, title FROM user_notifications
            WHERE type = :type
            ORDER BY user_id
        """),
        {"type": NotificationType.AGENT_DEACTIVATED.value},
    )
    notifications = notifications_result.fetchall()
    assert len(notifications) == 2
    assert "Test Agent" in notifications[0][2]


@pytest.mark.asyncio
async def test_set_group_default_agents_optimized(pg_session: AsyncSession):
    """Test bulk update only inserts added and deletes removed agents."""
    from playground_backend.repositories.sub_agent_repository import SubAgentRepository
    from playground_backend.repositories.user_group_repository import UserGroupRepository
    from playground_backend.services.audit_service import AuditService
    from playground_backend.services.notification_service import NotificationService
    from playground_backend.services.sub_agent_service import SubAgentService
    from playground_backend.services.user_group_service import UserGroupService

    # Setup
    audit_service = AuditService()
    user_group_repo = UserGroupRepository()
    sub_agent_repo = SubAgentRepository()
    sub_agent_repo.set_audit_service(audit_service)
    notification_service = NotificationService()
    sub_agent_service = SubAgentService(sub_agent_repository=sub_agent_repo, notification_service=notification_service)
    user_group_service = UserGroupService(
        user_group_repository=user_group_repo,
        sub_agent_service=sub_agent_service,
        notification_service=notification_service,
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status)
            VALUES ('user-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, 'user-1', 'manager')
        """)
    )

    # Create 3 sub-agents
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Agent 1', 'user-1', 'remote', 1),
                   (101, 'Agent 2', 'user-1', 'remote', 1),
                   (102, 'Agent 3', 'user-1', 'remote', 1)
        """)
    )

    # Add approved config versions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (
                sub_agent_id, version, description, agent_url, status, approved_at, release_number
            )
            VALUES (100, 1, 'Version 1', 'https://example.com/1', 'approved', NOW(), 1),
                   (101, 1, 'Version 1', 'https://example.com/2', 'approved', NOW(), 1),
                   (102, 1, 'Version 1', 'https://example.com/3', 'approved', NOW(), 1)
        """)
    )

    # Add permissions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions)
            VALUES (100, 1, ARRAY['read', 'write']),
                   (101, 1, ARRAY['read', 'write']),
                   (102, 1, ARRAY['read', 'write'])
        """)
    )

    # Set initial defaults: [100, 101]
    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 100, 'user-1'),
                   (1, 101, 'user-1')
        """)
    )

    # Create activations for initial defaults (use integer array not string array)
    await pg_session.execute(
        text("""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_by, activated_by_groups)
            VALUES ('user-1', 100, 'group', '[1]'::jsonb),
                   ('user-1', 101, 'group', '[1]'::jsonb)
        """)
    )
    await pg_session.commit()

    # Update to: [101, 102] (removed 100, kept 101, added 102)
    await user_group_service.set_group_default_agents(
        db=pg_session,
        group_id=1,
        sub_agent_ids=[101, 102],
        actor_sub="user-1",
    )
    await pg_session.commit()

    # Verify final state
    result = await pg_session.execute(
        text("""
            SELECT sub_agent_id FROM user_group_default_agents
            WHERE user_group_id = 1
            ORDER BY sub_agent_id
        """)
    )
    defaults = [row[0] for row in result.fetchall()]
    assert defaults == [101, 102]

    # Verify activations: 101 should still exist, 100 removed, 102 added
    activations_result = await pg_session.execute(
        text("""
            SELECT sub_agent_id FROM user_sub_agent_activations
            WHERE user_id = 'user-1'
            ORDER BY sub_agent_id
        """)
    )
    activations = [row[0] for row in activations_result.fetchall()]
    assert activations == [101, 102]

    # Verify notifications: 1 deactivation (100) and 1 activation (102)
    notifications_result = await pg_session.execute(
        text("""
            SELECT type, COUNT(*) FROM user_notifications
            GROUP BY type
            ORDER BY type
        """)
    )
    notification_counts = {row[0]: row[1] for row in notifications_result.fetchall()}
    assert notification_counts.get(NotificationType.AGENT_DEACTIVATED.value) == 1
    assert notification_counts.get(NotificationType.AGENT_ACTIVATED.value) == 1


@pytest.mark.asyncio
async def test_add_group_default_agent_idempotent(pg_session: AsyncSession):
    """Test adding an already-default agent is idempotent."""
    from playground_backend.repositories.sub_agent_repository import SubAgentRepository
    from playground_backend.repositories.user_group_repository import UserGroupRepository
    from playground_backend.services.audit_service import AuditService
    from playground_backend.services.notification_service import NotificationService
    from playground_backend.services.sub_agent_service import SubAgentService
    from playground_backend.services.user_group_service import UserGroupService

    # Setup
    audit_service = AuditService()
    user_group_repo = UserGroupRepository()
    sub_agent_repo = SubAgentRepository()
    sub_agent_repo.set_audit_service(audit_service)
    notification_service = NotificationService()
    sub_agent_service = SubAgentService(sub_agent_repository=sub_agent_repo, notification_service=notification_service)
    user_group_service = UserGroupService(
        user_group_repository=user_group_repo,
        sub_agent_service=sub_agent_service,
        notification_service=notification_service,
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status)
            VALUES ('user-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, 'user-1', 'manager')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Test Agent', 'user-1', 'remote', 1)
        """)
    )

    # Add approved config version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (
                sub_agent_id, version, description, agent_url, status, approved_at, release_number
            )
            VALUES (100, 1, 'Test Version', 'https://example.com', 'approved', NOW(), 1)
        """)
    )

    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions)
            VALUES (100, 1, ARRAY['read', 'write'])
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 100, 'user-1')
        """)
    )
    await pg_session.commit()

    # Add again (should be no-op)
    await user_group_service.add_group_default_agent(
        db=pg_session,
        group_id=1,
        sub_agent_id=100,
        actor_sub="user-1",
    )
    await pg_session.commit()

    # Verify still only one entry
    result = await pg_session.execute(
        text("""
            SELECT COUNT(*) FROM user_group_default_agents
            WHERE user_group_id = 1 AND sub_agent_id = 100
        """)
    )
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_remove_group_default_agent_idempotent(pg_session: AsyncSession):
    """Test removing a non-default agent is idempotent."""
    from playground_backend.repositories.sub_agent_repository import SubAgentRepository
    from playground_backend.repositories.user_group_repository import UserGroupRepository
    from playground_backend.services.audit_service import AuditService
    from playground_backend.services.notification_service import NotificationService
    from playground_backend.services.sub_agent_service import SubAgentService
    from playground_backend.services.user_group_service import UserGroupService

    # Setup
    audit_service = AuditService()
    user_group_repo = UserGroupRepository()
    sub_agent_repo = SubAgentRepository()
    sub_agent_repo.set_audit_service(audit_service)
    notification_service = NotificationService()
    sub_agent_service = SubAgentService(sub_agent_repository=sub_agent_repo, notification_service=notification_service)
    user_group_service = UserGroupService(
        user_group_repository=user_group_repo,
        sub_agent_service=sub_agent_service,
        notification_service=notification_service,
    )

    # Create test data (without default agent)
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status)
            VALUES ('user-1', 'sub-1', 'user1@test.com', 'User', 'One', 'member', 'active')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Test Agent', 'user-1', 'remote', 1)
        """)
    )

    # Add approved config version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (
                sub_agent_id, version, description, agent_url, status, approved_at, release_number
            )
            VALUES (100, 1, 'Test Version', 'https://example.com', 'approved', NOW(), 1)
        """)
    )

    await pg_session.commit()

    # Remove (should be no-op)
    await user_group_service.remove_group_default_agent(
        db=pg_session,
        group_id=1,
        sub_agent_id=100,
        actor_sub="user-1",
    )
    await pg_session.commit()

    # Verify still zero entries
    result = await pg_session.execute(
        text("""
            SELECT COUNT(*) FROM user_group_default_agents
            WHERE user_group_id = 1 AND sub_agent_id = 100
        """)
    )
    assert result.scalar() == 0
