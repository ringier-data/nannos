"""Repository for user operations with automatic audit logging."""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


class UserRepository(AuditedRepository):
    """Repository for user operations with automatic audit logging."""

    def __init__(self):
        super().__init__(entity_type=AuditEntityType.USER, table_name="users")

    async def update_status(
        self,
        db: AsyncSession,
        user_id: str,
        actor: User,
        new_status: str,
    ) -> None:
        """Update user status with automatic audit logging.

        Args:
            db: Database session
            user_id: ID of user to update
            actor: User performing the action
            new_status: New status value
        """
        await self.update(
            db=db,
            actor=actor,
            entity_id=user_id,
            fields={"status": new_status, "updated_at": datetime.now(timezone.utc)},
            fetch_before=True,
        )

    async def update_admin_fields(
        self,
        db: AsyncSession,
        user_id: str,
        actor: User,
        is_administrator: Optional[bool] = None,
    ) -> None:
        """Update user admin fields with automatic audit logging.

        Args:
            db: Database session
            user_id: ID of user to update
            actor: User performing the action
            is_administrator: New is_administrator value
        """
        if is_administrator is None:
            return

        await self.update(
            db=db,
            actor=actor,
            entity_id=user_id,
            fields={"is_administrator": is_administrator, "updated_at": datetime.now(timezone.utc)},
            fetch_before=True,
        )

    async def update_role(
        self,
        db: AsyncSession,
        user_id: str,
        actor: User,
        new_role: str,
    ) -> None:
        """Update user role with automatic audit logging.

        Args:
            db: Database session
            user_id: ID of user to update
            actor: User performing the action
            new_role: New role value
        """
        await self.update(
            db=db,
            actor=actor,
            entity_id=user_id,
            fields={"role": new_role, "updated_at": datetime.now(timezone.utc)},
            fetch_before=True,
        )

    async def update_groups(
        self,
        db: AsyncSession,
        user_id: str,
        actor: User,
        group_ids: List[int],
    ) -> None:
        """Update user's group memberships with automatic audit logging.

        Args:
            db: Database session
            user_id: ID of user to update
            actor: User performing the action
            group_ids: List of group IDs user should belong to
        """
        # Fetch before state
        result = await db.execute(
            text("""
                SELECT user_group_id 
                FROM user_group_members 
                WHERE user_id = :user_id
            """),
            {"user_id": user_id},
        )
        old_groups = [row[0] for row in result.fetchall()]

        # Delete existing memberships
        await db.execute(text("DELETE FROM user_group_members WHERE user_id = :user_id"), {"user_id": user_id})

        # Insert new memberships
        if group_ids:
            for group_id in group_ids:
                await db.execute(
                    text("""
                        INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                        VALUES (:group_id, :user_id, 'read', :created_at)
                    """),
                    {"group_id": group_id, "user_id": user_id, "created_at": datetime.now(timezone.utc)},
                )

        # Log audit
        await self.audit_service.log_action(
            db=db,
            entity_type=AuditEntityType.USER,
            entity_id=user_id,
            action=AuditAction.UPDATE,
            actor=actor,
            changes={"before": {"group_ids": old_groups}, "after": {"group_ids": group_ids}},
        )

    async def bulk_update_status(
        self,
        db: AsyncSession,
        user_id: str,
        actor: User,
        new_status: str,
    ) -> bool:
        """Update single user status as part of bulk operation.

        This method is designed to be called sequentially for each user
        in a bulk operation, with individual success/failure tracking.
        Each call gets its own audit log entry.

        Args:
            db: Database session (with autoflush disabled)
            user_id: ID of user to update
            actor: User performing the action
            new_status: New status value

        Returns:
            True if successful, False if user not found or error occurred
        """
        try:
            # Check if user exists first
            check_query = text(f"SELECT id FROM {self.table_name} WHERE id = :id")
            result = await db.execute(check_query, {"id": user_id})
            if not result.first():
                return False

            await self.update(
                db=db,
                actor=actor,
                entity_id=user_id,
                fields={"status": new_status, "updated_at": datetime.now(timezone.utc)},
                fetch_before=True,
            )
            return True

        except Exception as e:
            logger.error(f"Error updating user {user_id} status in bulk operation: {e}")
            return False
