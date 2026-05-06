"""Unit tests for group default agent management.

Tests cover:
- add_group_default_agent() - adding a single default agent
- remove_group_default_agent() - removing a single default agent
- set_group_default_agents() - optimized bulk update with added/removed tracking
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from console_backend.models.notification import NotificationType
from console_backend.models.user import User
from console_backend.services.sub_agent_service import SubAgentService
from console_backend.services.user_group_service import UserGroupService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_add_group_default_agent(
    pg_session: AsyncSession, test_user_db: User, test_approver_user_db: User, user_group_service: UserGroupService
):
    """Test adding a single default agent to a group activates it for all members."""
    # Create test group
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )

    # Add members to group
    await pg_session.execute(
        text(f"""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, {test_approver_user_db.id!r}, 'manager'),
                   (1, {test_user_db.id!r}, 'read')
        """)
    )

    # Create sub-agent
    await pg_session.execute(
        text(f"""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Test Agent', {test_approver_user_db.id!r}, 'remote', 1)
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
        actor=test_approver_user_db,
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
    assert activations[0][0] == test_approver_user_db.id
    assert activations[0][2] == "group"
    assert activations[1][0] == test_user_db.id
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
async def test_remove_group_default_agent(
    pg_session: AsyncSession, test_user_db: User, test_approver_user_db: User, user_group_service: UserGroupService
):
    """Test removing a single default agent deactivates it for all members."""

    # Create group
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )
    await pg_session.execute(
        text(f"""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, {test_approver_user_db.id!r}, 'manager'),
                   (1, {test_user_db.id!r}, 'read')
        """)
    )

    # Create sub-agent and make it a default
    await pg_session.execute(
        text(f"""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Test Agent', {test_approver_user_db.id!r}, 'remote', 1)
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
        text(f"""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 100, {test_approver_user_db.id!r})
        """)
    )

    # Create activations for both users (use integer array not string array)
    await pg_session.execute(
        text(f"""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_by, activated_by_groups)
            VALUES ({test_approver_user_db.id!r}, 100, 'group', '[1]'::jsonb),
                   ({test_user_db.id!r}, 100, 'group', '[1]'::jsonb)
        """)
    )
    await pg_session.commit()

    # Remove the default agent
    await user_group_service.remove_group_default_agent(
        db=pg_session,
        group_id=1,
        sub_agent_id=100,
        actor=test_approver_user_db,
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
async def test_set_group_default_agents_optimized(
    pg_session: AsyncSession,
    test_user_db: User,
    user_group_service: UserGroupService,
):
    """Test bulk update only inserts added and deletes removed agents."""

    # Create test data

    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )
    await pg_session.execute(
        text(f"""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, {test_user_db.id!r}, 'manager')
        """)
    )

    # Create 3 sub-agents
    await pg_session.execute(
        text(f"""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Agent 1', {test_user_db.id!r}, 'remote', 1),
                   (101, 'Agent 2', {test_user_db.id!r}, 'remote', 1),
                   (102, 'Agent 3', {test_user_db.id!r}, 'remote', 1)
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
        text(f"""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 100, {test_user_db.id!r}),
                   (1, 101, {test_user_db.id!r})
        """)
    )

    # Create activations for initial defaults (use integer array not string array)
    await pg_session.execute(
        text(f"""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_by, activated_by_groups)
            VALUES ({test_user_db.id!r}, 100, 'group', '[1]'::jsonb),
                   ({test_user_db.id!r}, 101, 'group', '[1]'::jsonb)
        """)
    )
    await pg_session.commit()

    # Update to: [101, 102] (removed 100, kept 101, added 102)
    await user_group_service.set_group_default_agents(
        db=pg_session,
        group_id=1,
        sub_agent_ids=[101, 102],
        actor=test_user_db,
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
        text(f"""
            SELECT sub_agent_id FROM user_sub_agent_activations
            WHERE user_id = {test_user_db.id!r}
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
async def test_add_group_default_agent_idempotent(
    pg_session: AsyncSession,
    test_user_db: User,
    user_group_service: UserGroupService,
    sub_agent_service: SubAgentService,
):
    """Test adding an already-default agent is idempotent."""

    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )
    await pg_session.execute(
        text(f"""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, {test_user_db.id!r}, 'manager')
        """)
    )
    await pg_session.execute(
        text(f"""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Test Agent', {test_user_db.id!r}, 'remote', 1)
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
        text(f"""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 100, {test_user_db.id!r})
        """)
    )
    await pg_session.commit()

    # Add again (should be no-op)
    await user_group_service.add_group_default_agent(
        db=pg_session,
        group_id=1,
        sub_agent_id=100,
        actor=test_user_db,
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
async def test_remove_group_default_agent_idempotent(
    pg_session: AsyncSession, test_user_db: User, user_group_service: UserGroupService
):
    """Test removing a non-default agent is idempotent."""

    # Create test data (without default agent)
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test Description')
        """)
    )
    await pg_session.execute(
        text(f"""
            INSERT INTO sub_agents (id, name, owner_user_id, type, default_version)
            VALUES (100, 'Test Agent', {test_user_db.id!r}, 'remote', 1)
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
        actor=test_user_db,
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
