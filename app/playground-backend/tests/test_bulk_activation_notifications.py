"""Unit tests for bulk activation notification filtering.

Tests verify that notifications are only sent to users whose activation state
actually changed, not to users who already had the agent activated.
"""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.notification import NotificationType


@pytest.mark.asyncio
async def test_bulk_activate_only_notifies_affected_users(pg_session: AsyncSession):
    """Test that bulk activation only sends notifications to users whose state changed."""
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
            VALUES ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member', 'active'),
                   ('user-2', 'user-2', 'user2@test.com', 'User', 'Two', 'member', 'active'),
                   ('user-3', 'user-3', 'user3@test.com', 'User', 'Three', 'member', 'active')
        """)
    )

    # Create a test group with all three users
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test group')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, 'user-1', 'manager'),
                   (1, 'user-2', 'write'),
                   (1, 'user-3', 'write')
        """)
    )

    # Create an approved sub-agent with version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, owner_user_id, type, current_version, default_version)
            VALUES (1, 'Test Agent', 'user-1', 'local', 1, 1)
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (sub_agent_id, version, description, system_prompt, model, status, approved_by_user_id)
            VALUES (1, 1, 'Test agent', 'test prompt', 'gpt-4', 'approved', 'user-1')
        """)
    )

    # Give group permissions to the agent
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions)
            VALUES (1, 1, ARRAY['read', 'write']::TEXT[])
        """)
    )

    # Pre-activate the agent for user-1 (simulate they already have it activated)
    await pg_session.execute(
        text("""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_at, activated_by)
            VALUES ('user-1', 1, NOW(), 'user')
        """)
    )

    await pg_session.commit()

    # Clear any existing notifications
    await pg_session.execute(text("DELETE FROM user_notifications"))
    await pg_session.commit()

    # Add the agent as a default for the group
    # This should activate it for user-2 and user-3, but NOT user-1 (already activated)
    await user_group_service.add_group_default_agent(
        db=pg_session,
        group_id=1,
        sub_agent_id=1,
        actor_sub="user-1",
    )
    await pg_session.commit()

    # Verify notifications were only sent to user-2 and user-3
    result = await pg_session.execute(text("SELECT user_id, type FROM user_notifications ORDER BY user_id"))
    notifications = result.mappings().all()

    # Should only have notifications for user-2 and user-3, NOT user-1
    assert len(notifications) == 2
    assert notifications[0]["user_id"] == "user-2"
    assert notifications[0]["type"] == NotificationType.AGENT_ACTIVATED.value
    assert notifications[1]["user_id"] == "user-3"
    assert notifications[1]["type"] == NotificationType.AGENT_ACTIVATED.value


@pytest.mark.asyncio
async def test_bulk_deactivate_only_notifies_affected_users(pg_session: AsyncSession):
    """Test that bulk deactivation only sends notifications to users whose state changed."""
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
            VALUES ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member', 'active'),
                   ('user-2', 'user-2', 'user2@test.com', 'User', 'Two', 'member', 'active'),
                   ('user-3', 'user-3', 'user3@test.com', 'User', 'Three', 'member', 'active')
        """)
    )

    # Create a test group with all three users
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test group')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, 'user-1', 'manager'),
                   (1, 'user-2', 'write'),
                   (1, 'user-3', 'write')
        """)
    )

    # Create an approved sub-agent with version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, owner_user_id, type, current_version, default_version)
            VALUES (1, 'Test Agent', 'user-1', 'local', 1, 1)
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (sub_agent_id, version, description, system_prompt, model, status, approved_by_user_id)
            VALUES (1, 1, 'Test agent', 'test prompt', 'gpt-4', 'approved', 'user-1')
        """)
    )

    # Give group permissions to the agent
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions)
            VALUES (1, 1, ARRAY['read', 'write']::TEXT[])
        """)
    )

    # Activate the agent for user-2 and user-3 via group, but leave user-1 without it
    await pg_session.execute(
        text("""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_at, activated_by, activated_by_groups)
            VALUES ('user-2', 1, NOW(), 'group', '[1]'::jsonb),
                   ('user-3', 1, NOW(), 'group', '[1]'::jsonb)
        """)
    )

    # Add it as default
    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 1, 'user-1')
        """)
    )

    await pg_session.commit()

    # Clear any existing notifications
    await pg_session.execute(text("DELETE FROM user_notifications"))
    await pg_session.commit()

    # Remove the agent from defaults
    # This should deactivate it for user-2 and user-3, but NOT user-1 (never activated)
    await user_group_service.remove_group_default_agent(
        db=pg_session,
        group_id=1,
        sub_agent_id=1,
        actor_sub="user-1",
    )
    await pg_session.commit()

    # Verify notifications were only sent to user-2 and user-3
    result = await pg_session.execute(text("SELECT user_id, type FROM user_notifications ORDER BY user_id"))
    notifications = result.mappings().all()

    # Should only have notifications for user-2 and user-3, NOT user-1
    assert len(notifications) == 2
    assert notifications[0]["user_id"] == "user-2"
    assert notifications[0]["type"] == NotificationType.AGENT_DEACTIVATED.value
    assert notifications[1]["user_id"] == "user-3"
    assert notifications[1]["type"] == NotificationType.AGENT_DEACTIVATED.value


@pytest.mark.asyncio
async def test_add_members_only_notifies_members_with_new_activations(pg_session: AsyncSession):
    """Test that adding members only notifies those who get new agent activations."""
    from playground_backend.repositories.sub_agent_repository import SubAgentRepository
    from playground_backend.repositories.user_group_repository import UserGroupRepository
    from playground_backend.services.audit_service import AuditService
    from playground_backend.services.notification_service import NotificationService
    from playground_backend.services.sub_agent_service import SubAgentService
    from playground_backend.services.user_group_service import UserGroupService

    # Setup repositories and services
    audit_service = AuditService()
    user_group_repo = UserGroupRepository()
    user_group_repo.set_audit_service(audit_service)
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
            VALUES ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member', 'active'),
                   ('user-2', 'user-2', 'user2@test.com', 'User', 'Two', 'member', 'active'),
                   ('user-3', 'user-3', 'user3@test.com', 'User', 'Three', 'member', 'active')
        """)
    )

    # Create a test group with user-1
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Test group for add members tests')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, 'user-1', 'manager')
        """)
    )

    # Create an approved sub-agent with version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, owner_user_id, type, current_version, default_version)
            VALUES (1, 'Test Agent', 'user-1', 'local', 1, 1)
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (sub_agent_id, version, description, system_prompt, model, status, approved_by_user_id)
            VALUES (1, 1, 'Test agent', 'test prompt', 'gpt-4', 'approved', 'user-1')
        """)
    )

    # Give group permissions to the agent
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (sub_agent_id, user_group_id, permissions)
            VALUES (1, 1, ARRAY['read', 'write']::TEXT[])
        """)
    )

    # Set agent as default
    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 1, 'user-1')
        """)
    )

    # Pre-activate the agent for user-2 (they already have it from another source)
    await pg_session.execute(
        text("""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_at, activated_by)
            VALUES ('user-2', 1, NOW(), 'user')
        """)
    )

    await pg_session.commit()

    # Clear any existing notifications
    await pg_session.execute(text("DELETE FROM user_notifications"))
    await pg_session.commit()

    # Add user-2 and user-3 to the group
    # user-2 already has the agent activated, user-3 doesn't
    # Only user-3 should get a notification
    await user_group_service.add_members(
        db=pg_session,
        group_id=1,
        user_ids=["user-2", "user-3"],
        role="write",
        actor_sub="user-1",
    )
    await pg_session.commit()

    # Verify only user-3 got notified about agent activation
    result = await pg_session.execute(
        text("""
            SELECT user_id, type 
            FROM user_notifications 
            WHERE type = :notif_type
            ORDER BY user_id
        """),
        {"notif_type": NotificationType.AGENT_ACTIVATED.value},
    )
    notifications = result.mappings().all()

    # Should only have notification for user-3, NOT user-2 (already activated)
    assert len(notifications) == 1
    assert notifications[0]["user_id"] == "user-3"
    assert notifications[0]["type"] == NotificationType.AGENT_ACTIVATED.value
