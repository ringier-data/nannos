"""Tests for notification service."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.notification import NotificationType
from playground_backend.services.notification_service import NotificationService


@pytest.mark.asyncio
async def test_create_notification(pg_session: AsyncSession):
    """Test creating a single notification."""
    service = NotificationService()

    # Create user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('user-1', 'user-sub-1', 'user1@test.com', 'User', 'One', 'member')
        """)
    )
    await pg_session.commit()

    # Create notification
    notif_id = await service.create_notification(
        db=pg_session,
        user_id="user-1",
        notification_type=NotificationType.AGENT_ACTIVATED,
        title="Test Agent Enabled",
        message="Test agent was enabled for you",
        metadata={"agent_id": 123},
    )
    await pg_session.commit()

    # Verify
    result = await pg_session.execute(
        text("""
            SELECT user_id, type, title, message, read_at, metadata
            FROM user_notifications
            WHERE id = :id
        """),
        {"id": notif_id},
    )
    row = result.mappings().first()
    assert row is not None

    assert row["user_id"] == "user-1"
    assert row["type"] == "agent_activated"
    assert row["title"] == "Test Agent Enabled"
    assert row["message"] == "Test agent was enabled for you"
    assert row["read_at"] is None
    assert row["metadata"] == {"agent_id": 123}


@pytest.mark.asyncio
async def test_bulk_create_notifications(pg_session: AsyncSession):
    """Test bulk creating notifications."""
    service = NotificationService()

    # Create users
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES 
                ('user-1', 'user-sub-1', 'user1@test.com', 'User', 'One', 'member'),
                ('user-2', 'user-sub-2', 'user2@test.com', 'User', 'Two', 'member'),
                ('user-3', 'user-sub-3', 'user3@test.com', 'User', 'Three', 'member')
        """)
    )
    await pg_session.commit()

    # Bulk create
    from playground_backend.models.notification import NotificationData

    notifications = [
        NotificationData(
            user_id="user-1",
            notification_type=NotificationType.AGENT_ACTIVATED,
            title="Agent Enabled",
            message="Agent was enabled",
            metadata={"agent_id": 1},
        ),
        NotificationData(
            user_id="user-2",
            notification_type=NotificationType.AGENT_ACTIVATED,
            title="Agent Enabled",
            message="Agent was enabled",
            metadata={"agent_id": 1},
        ),
        NotificationData(
            user_id="user-3",
            notification_type=NotificationType.AGENT_ACTIVATED,
            title="Agent Enabled",
            message="Agent was enabled",
            metadata={"agent_id": 1},
        ),
    ]

    await service.bulk_create_notifications(pg_session, notifications)
    await pg_session.commit()

    # Verify
    result = await pg_session.execute(
        text("""
            SELECT COUNT(*) FROM user_notifications
            WHERE type = 'agent_activated'
        """)
    )
    assert result.scalar() == 3


@pytest.mark.asyncio
async def test_get_user_notifications(pg_session: AsyncSession):
    """Test retrieving user notifications with pagination."""
    service = NotificationService()

    # Create user and notifications
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('user-1', 'user-sub-1', 'user1@test.com', 'User', 'One', 'member')
        """)
    )

    for i in range(5):
        await service.create_notification(
            db=pg_session,
            user_id="user-1",
            notification_type=NotificationType.AGENT_ACTIVATED,
            title=f"Notification {i}",
            message=f"Message {i}",
        )

    await pg_session.commit()

    # Get page 1
    notifications, total = await service.get_user_notifications(
        db=pg_session,
        user_id="user-1",
        page=1,
        limit=3,
    )

    assert len(notifications) == 3
    assert total == 5
    assert notifications[0].title == "Notification 4"  # Newest first
    assert total == 5


@pytest.mark.asyncio
async def test_get_user_notifications_unread_only(pg_session: AsyncSession):
    """Test filtering notifications by unread status."""
    service = NotificationService()

    # Create user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('user-1', 'user-sub-1', 'user1@test.com', 'User', 'One', 'member')
        """)
    )
    await pg_session.commit()

    # Create notifications
    notif_ids = []
    for i in range(5):
        notif_id = await service.create_notification(
            db=pg_session,
            user_id="user-1",
            notification_type=NotificationType.AGENT_ACTIVATED,
            title=f"Notification {i}",
            message=f"Message {i}",
        )
        notif_ids.append(notif_id)
    await pg_session.commit()

    # Mark some as read
    await service.mark_as_read(pg_session, [notif_ids[0], notif_ids[1]], "user-1")
    await pg_session.commit()

    # Get unread only
    notifications, total = await service.get_user_notifications(
        db=pg_session,
        user_id="user-1",
        unread_only=True,
    )

    assert len(notifications) == 3
    assert total == 3
    assert all(n.read_at is None for n in notifications)


@pytest.mark.asyncio
async def test_mark_as_read(pg_session: AsyncSession):
    """Test marking notifications as read."""
    service = NotificationService()

    # Create user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('user-1', 'user-sub-1', 'user1@test.com', 'User', 'One', 'member')
        """)
    )
    await pg_session.commit()

    # Create notifications
    notif_ids = []
    for i in range(3):
        notif_id = await service.create_notification(
            db=pg_session,
            user_id="user-1",
            notification_type=NotificationType.AGENT_ACTIVATED,
            title=f"Notification {i}",
            message=f"Message {i}",
        )
        notif_ids.append(notif_id)
    await pg_session.commit()

    # Mark as read
    updated = await service.mark_as_read(pg_session, [notif_ids[0], notif_ids[1]], "user-1")
    await pg_session.commit()

    assert updated == 2

    # Verify
    result = await pg_session.execute(
        text("""
            SELECT id, read_at FROM user_notifications
            WHERE user_id = 'user-1'
            ORDER BY id
        """)
    )
    rows = result.fetchall()

    assert rows[0][1] is not None  # Read
    assert rows[1][1] is not None  # Read
    assert rows[2][1] is None  # Unread


@pytest.mark.asyncio
async def test_mark_all_as_read(pg_session: AsyncSession):
    """Test marking all notifications as read."""
    service = NotificationService()

    # Create user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('user-1', 'user-sub-1', 'user1@test.com', 'User', 'One', 'member')
        """)
    )
    await pg_session.commit()

    # Create notifications
    for i in range(5):
        await service.create_notification(
            db=pg_session,
            user_id="user-1",
            notification_type=NotificationType.AGENT_ACTIVATED,
            title=f"Notification {i}",
            message=f"Message {i}",
        )
    await pg_session.commit()

    # Mark all as read
    updated = await service.mark_all_as_read(pg_session, "user-1")
    await pg_session.commit()

    assert updated == 5

    # Verify
    result = await pg_session.execute(
        text("""
            SELECT COUNT(*) FROM user_notifications
            WHERE user_id = 'user-1' AND read_at IS NOT NULL
        """)
    )
    assert result.scalar() == 5


@pytest.mark.asyncio
async def test_get_unread_count(pg_session: AsyncSession):
    """Test getting unread notification count."""
    service = NotificationService()

    # Create user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('user-1', 'user-sub-1', 'user1@test.com', 'User', 'One', 'member')
        """)
    )
    await pg_session.commit()

    # Initial count
    count = await service.get_unread_count(pg_session, "user-1")
    assert count == 0

    # Create notifications
    notif_ids = []
    for i in range(5):
        notif_id = await service.create_notification(
            db=pg_session,
            user_id="user-1",
            notification_type=NotificationType.AGENT_ACTIVATED,
            title=f"Notification {i}",
            message=f"Message {i}",
        )
        notif_ids.append(notif_id)
    await pg_session.commit()

    # Count after creation
    count = await service.get_unread_count(pg_session, "user-1")
    assert count == 5

    # Mark some as read
    await service.mark_as_read(pg_session, [notif_ids[0], notif_ids[1]], "user-1")
    await pg_session.commit()

    # Count after marking some read
    count = await service.get_unread_count(pg_session, "user-1")
    assert count == 3
