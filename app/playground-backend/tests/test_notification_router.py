"""Integration tests for notification router."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_notifications_empty(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test getting notifications when there are none."""
    response = await client_with_db.get("/api/v1/notifications")

    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["unread_count"] == 0


@pytest.mark.asyncio
async def test_get_notifications_with_data(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test getting notifications with pagination."""
    # Create notifications
    now = datetime.now(timezone.utc)
    for i in range(25):
        now = now + timedelta(minutes=1)

        await pg_session.execute(
            text("""
                INSERT INTO user_notifications (user_id, type, title, message, created_at, read_at, metadata)
                VALUES (:user_id, 'agent_activated', :title, :message, :created_at, :read_at, '{}')
            """),
            {
                "user_id": test_user_model.id,
                "title": f"Notification {i}",
                "message": f"Message {i}",
                "created_at": now,
                "read_at": now if i < 10 else None,  # First 10 are read
            },
        )
    await pg_session.commit()

    # Get first page
    response = await client_with_db.get("/api/v1/notifications?page=1&limit=10")

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 10
    assert data["total"] == 25
    assert data["unread_count"] == 15

    # Verify sorting (newest first)
    assert data["items"][0]["title"] == "Notification 24"

    # Get second page
    response = await client_with_db.get("/api/v1/notifications?page=2&limit=10")

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 10
    assert data["total"] == 25
    assert data["unread_count"] == 15


@pytest.mark.asyncio
async def test_get_notifications_unread_only(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test filtering notifications by unread status."""
    # Create notifications
    now = datetime.now(timezone.utc)
    for i in range(10):
        now = now + timedelta(minutes=1)
        await pg_session.execute(
            text("""
                INSERT INTO user_notifications (user_id, type, title, message, created_at, read_at, metadata)
                VALUES (:user_id, 'agent_activated', :title, :message, :created_at, :read_at, '{}')
            """),
            {
                "user_id": test_user_model.id,
                "title": f"Notification {i}",
                "message": f"Message {i}",
                "read_at": now if i < 5 else None,
                "created_at": now,
            },
        )
    await pg_session.commit()

    # Get unread only
    response = await client_with_db.get("/api/v1/notifications?unread_only=true")

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 5
    assert data["total"] == 5
    assert all(not n["read_at"] for n in data["items"])


@pytest.mark.asyncio
async def test_get_unread_count(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test getting unread notification count."""
    # Initially no notifications
    response = await client_with_db.get("/api/v1/notifications/unread-count")

    assert response.status_code == 200
    assert response.json()["count"] == 0

    # Create notifications
    now = datetime.now(timezone.utc)
    for i in range(10):
        now = now + timedelta(minutes=1)
        await pg_session.execute(
            text("""
                INSERT INTO user_notifications (user_id, type, title, message, created_at, read_at, metadata)
                VALUES (:user_id, 'agent_activated', :title, :message, :created_at, :read_at, '{}')
            """),
            {
                "user_id": test_user_model.id,
                "title": f"Notification {i}",
                "message": f"Message {i}",
                "read_at": now if i < 3 else None,  # First 3 are read
                "created_at": now,
            },
        )
    await pg_session.commit()

    # Get count
    response = await client_with_db.get("/api/v1/notifications/unread-count")

    assert response.status_code == 200
    assert response.json()["count"] == 7


@pytest.mark.asyncio
async def test_mark_notifications_as_read(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test marking specific notifications as read."""
    # Create notifications
    notif_ids = []
    now = datetime.now(timezone.utc)
    for i in range(5):
        now = now + timedelta(minutes=1)
        result = await pg_session.execute(
            text("""
                INSERT INTO user_notifications (user_id, type, title, message, created_at, metadata)
                VALUES (:user_id, 'agent_activated', :title, :message, :created_at, '{}')
                RETURNING id
            """),
            {
                "user_id": test_user_model.id,
                "title": f"Notification {i}",
                "message": f"Message {i}",
                "created_at": now,
            },
        )
        notif_ids.append(result.scalar())
    await pg_session.commit()

    # Mark first 3 as read
    response = await client_with_db.put(
        "/api/v1/notifications/mark-read",
        json={"notification_ids": notif_ids[:3]},
    )

    assert response.status_code == 200

    # Verify
    result = await pg_session.execute(
        text("""
            SELECT COUNT(*) FROM user_notifications
            WHERE user_id = :user_id AND read_at IS NOT NULL
        """),
        {"user_id": test_user_model.id},
    )
    assert result.scalar() == 3


@pytest.mark.asyncio
async def test_mark_notifications_as_read_ignores_other_users(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Test that marking as read only affects current user's notifications."""
    # Create another user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('other-user', 'other-sub', 'other@test.com', 'Other', 'User', 'member')
        """)
    )

    # Create notification for other user
    result = await pg_session.execute(
        text("""
            INSERT INTO user_notifications (user_id, type, title, message, metadata)
            VALUES ('other-user', 'agent_activated', 'Other Notification', 'Message', '{}')
            RETURNING id
        """)
    )
    other_notif_id = result.scalar()
    await pg_session.commit()

    # Try to mark other user's notification as read
    response = await client_with_db.put(
        "/api/v1/notifications/mark-read",
        json={"notification_ids": [other_notif_id]},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_mark_all_as_read(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test marking all notifications as read."""
    # Create notifications
    for i in range(10):
        await pg_session.execute(
            text("""
                INSERT INTO user_notifications (user_id, type, title, message, metadata)
                VALUES (:user_id, 'agent_activated', :title, :message, '{}')
            """),
            {
                "user_id": test_user_model.id,
                "title": f"Notification {i}",
                "message": f"Message {i}",
            },
        )
    await pg_session.commit()

    # Mark all as read
    response = await client_with_db.put("/api/v1/notifications/mark-all-read")

    assert response.status_code == 200

    # Verify
    result = await pg_session.execute(
        text("""
            SELECT COUNT(*) FROM user_notifications
            WHERE user_id = :user_id AND read_at IS NOT NULL
        """),
        {"user_id": test_user_model.id},
    )
    assert result.scalar() == 10


@pytest.mark.asyncio
async def test_mark_all_as_read_ignores_other_users(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Test that marking all as read only affects current user."""
    # Create another user with notifications
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('other-user', 'other-sub', 'other@test.com', 'Other', 'User', 'member')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_notifications (user_id, type, title, message, metadata)
            VALUES ('other-user', 'agent_activated', 'Other Notification', 'Message', '{}')
        """)
    )

    # Create current user notifications
    for i in range(5):
        await pg_session.execute(
            text("""
                INSERT INTO user_notifications (user_id, type, title, message, metadata)
                VALUES (:user_id, 'agent_activated', :title, :message, '{}')
            """),
            {
                "user_id": test_user_model.id,
                "title": f"Notification {i}",
                "message": f"Message {i}",
            },
        )
    await pg_session.commit()

    # Mark all as read
    response = await client_with_db.put("/api/v1/notifications/mark-all-read")

    assert response.status_code == 200

    # Verify other user's notification is still unread
    result = await pg_session.execute(
        text("""
            SELECT read_at FROM user_notifications
            WHERE user_id = 'other-user'
        """)
    )
    assert result.scalar() is None


@pytest.mark.asyncio
async def test_notifications_require_authentication(client: AsyncClient):
    """Test that all notification endpoints require authentication."""
    # Get notifications
    response = await client.get("/api/v1/notifications")
    assert response.status_code == 401

    # Get unread count
    response = await client.get("/api/v1/notifications/unread-count")
    assert response.status_code == 401

    # Mark as read
    response = await client.put(
        "/api/v1/notifications/mark-read",
        json={"notification_ids": [1]},
    )
    assert response.status_code == 401

    # Mark all as read
    response = await client.put("/api/v1/notifications/mark-all-read")
    assert response.status_code == 401
