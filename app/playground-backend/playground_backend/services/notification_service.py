"""Service for managing user notifications."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.notification import (
    NotificationData,
    NotificationType,
    UserNotification,
)

logger = logging.getLogger(__name__)


class NotificationService:
    """Service for managing user notifications."""

    async def create_notification(
        self,
        db: AsyncSession,
        user_id: str,
        notification_type: NotificationType,
        title: str,
        message: str,
        metadata: dict | None = None,
    ) -> int:
        """
        Create a new notification for a user.

        Args:
            db: Database session
            user_id: User ID
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            metadata: Optional metadata dict

        Returns:
            Created notification ID
        """
        query = text("""
            INSERT INTO user_notifications (user_id, type, title, message, metadata, created_at)
            VALUES (:user_id, :type, :title, :message, :metadata, :created_at)
            RETURNING id
        """)

        result = await db.execute(
            query,
            {
                "user_id": user_id,
                "type": notification_type.value,
                "title": title,
                "message": message,
                "metadata": json.dumps(metadata) if metadata else json.dumps({}),
                "created_at": datetime.now(timezone.utc),
            },
        )

        notification_id = result.scalar_one()
        logger.info(f"Created notification {notification_id} for user {user_id}: {title}")
        return notification_id

    async def bulk_create_notifications(
        self,
        db: AsyncSession,
        notifications: list[NotificationData],
    ) -> list[int]:
        """
        Create multiple notifications in a single query.

        Args:
            db: Database session
            notifications: List of NotificationData models

        Returns:
            List of created notification IDs
        """

        if not notifications:
            return []

        # Build VALUES clause for bulk insert
        values = []
        params = {}
        for i, notif in enumerate(notifications):
            values.append(f"(:user_id_{i}, :type_{i}, :title_{i}, :message_{i}, :metadata_{i}, :created_at_{i})")
            params[f"user_id_{i}"] = notif.user_id
            params[f"type_{i}"] = notif.notification_type.value
            params[f"title_{i}"] = notif.title
            params[f"message_{i}"] = notif.message
            # JSON-serialize the metadata dict for JSONB column
            params[f"metadata_{i}"] = json.dumps(notif.metadata) if notif.metadata else None
            params[f"created_at_{i}"] = datetime.now(timezone.utc)

        query = text(f"""
            INSERT INTO user_notifications (user_id, type, title, message, metadata, created_at)
            VALUES {", ".join(values)}
            RETURNING id
        """)

        result = await db.execute(query, params)
        notification_ids = [row[0] for row in result]
        logger.info(f"Created {len(notification_ids)} notifications in bulk")
        return notification_ids

    async def get_user_notifications(
        self,
        db: AsyncSession,
        user_id: str,
        unread_only: bool = False,
        page: int = 1,
        limit: int = 20,
    ) -> tuple[list[UserNotification], int]:
        """
        Get notifications for a user with pagination.

        Args:
            db: Database session
            user_id: User ID
            unread_only: If True, only return unread notifications
            page: Page number (1-indexed)
            limit: Items per page

        Returns:
            Tuple of (notifications, total_count)
        """
        read_filter = "AND read_at IS NULL" if unread_only else ""

        count_query = text(f"""
            SELECT COUNT(*) as total
            FROM user_notifications
            WHERE user_id = :user_id {read_filter}
        """)

        data_query = text(f"""
            SELECT id, user_id, type, title, message, metadata, read_at, created_at
            FROM user_notifications
            WHERE user_id = :user_id {read_filter}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)

        params = {
            "user_id": user_id,
            "limit": limit,
            "offset": (page - 1) * limit,
        }

        count_result = await db.execute(count_query, params)
        total = count_result.scalar() or 0

        data_result = await db.execute(data_query, params)
        rows = data_result.mappings().all()

        notifications = [
            UserNotification(
                id=row["id"],
                user_id=row["user_id"],
                type=NotificationType(row["type"]),
                title=row["title"],
                message=row["message"],
                metadata=row["metadata"],
                read_at=row["read_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

        return notifications, total

    async def mark_as_read(
        self,
        db: AsyncSession,
        notification_ids: list[int],
        user_id: str,
    ) -> int:
        """
        Mark notifications as read (bulk operation).

        Args:
            db: Database session
            notification_ids: List of notification IDs
            user_id: User ID (for security check)

        Returns:
            Number of notifications marked as read
        """
        if not notification_ids:
            return 0

        query = text("""
            UPDATE user_notifications
            SET read_at = :read_at
            WHERE id = ANY(:ids) AND user_id = :user_id AND read_at IS NULL
            RETURNING id
        """)

        result = await db.execute(
            query,
            {
                "ids": list(set(notification_ids)),
                "user_id": user_id,
                "read_at": datetime.now(timezone.utc),
            },
        )

        count = len(result.fetchall())
        logger.info(f"Marked {count} notifications as read for user {user_id}")
        return count

    async def mark_all_as_read(
        self,
        db: AsyncSession,
        user_id: str,
    ) -> int:
        """
        Mark all unread notifications as read for a user.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            Number of notifications marked as read
        """
        query = text("""
            UPDATE user_notifications
            SET read_at = :read_at
            WHERE user_id = :user_id AND read_at IS NULL
            RETURNING id
        """)

        result = await db.execute(
            query,
            {
                "user_id": user_id,
                "read_at": datetime.now(timezone.utc),
            },
        )

        count = len(result.fetchall())
        logger.info(f"Marked all {count} notifications as read for user {user_id}")
        return count

    async def get_unread_count(
        self,
        db: AsyncSession,
        user_id: str,
    ) -> int:
        """
        Get count of unread notifications for a user.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            Count of unread notifications
        """
        query = text("""
            SELECT COUNT(*) as count
            FROM user_notifications
            WHERE user_id = :user_id AND read_at IS NULL
        """)

        result = await db.execute(query, {"user_id": user_id})
        return result.scalar() or 0
