"""Repository for user group operations with automatic audit logging."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


class UserGroupRepository(AuditedRepository):
    """Repository for user group operations with automatic audit logging."""

    def __init__(self):
        super().__init__(entity_type=AuditEntityType.GROUP, table_name="user_groups")

    async def add_members(
        self,
        db: AsyncSession,
        actor: User,
        group_id: int,
        member_additions: list[dict[str, str]],
    ) -> list[str]:
        """
        Add members to a group with audit logging.

        Args:
            db: Database session
            actor: Actor context with user_id (for FK) and sub (for audit)
            group_id: Group ID
            member_additions: List of dicts with user_id and role

        Returns:
            List of user IDs that were added (or on conflict updated)
        """
        try:
            # Insert members
            for member in member_additions:
                await db.execute(
                    text("""
                        INSERT INTO user_group_members (user_id, user_group_id, group_role)
                        VALUES (:user_id, :group_id, :role)
                        ON CONFLICT (user_id, user_group_id) DO UPDATE SET group_role = :role
                    """),
                    {
                        "user_id": member["user_id"],
                        "group_id": group_id,
                        "role": member["role"],
                    },
                )

            # Log audit
            await self.audit_service.log_action(
                db=db,
                actor=actor,
                entity_type=self.entity_type,
                entity_id=str(group_id),
                action=AuditAction.ASSIGN,
                changes={
                    "members_added": member_additions,
                },
            )

            logger.info(f"Added {len(member_additions)} members to group {group_id} by {actor.sub}")
            return [member["user_id"] for member in member_additions]
        except Exception as e:
            logger.error(f"Failed to add members to group {group_id}: {e}")
            raise

    async def remove_members(
        self,
        db: AsyncSession,
        actor: User,
        group_id: int,
        user_ids: list[str],
    ) -> list[str]:
        """
        Remove multiple members from a group with audit logging.

        Args:
            db: Database session
            actor: Actor context with user_id (for FK) and sub (for audit)
            group_id: Group ID
            user_ids: List of user IDs to remove

        Returns:
            List of user IDs that were actually removed
        """
        try:
            removed: list[str] = []

            # Remove members and track which ones existed
            for user_id in user_ids:
                result = await db.execute(
                    text("""
                        DELETE FROM user_group_members
                        WHERE user_group_id = :group_id AND user_id = :user_id
                    """),
                    {"group_id": group_id, "user_id": user_id},
                )
                if result.rowcount > 0:  # type: ignore
                    removed.append(user_id)

            # Log audit only for successfully removed members
            if removed:
                await self.audit_service.log_action(
                    db=db,
                    actor=actor,
                    entity_type=self.entity_type,
                    entity_id=str(group_id),
                    action=AuditAction.UNASSIGN,
                    changes={
                        "members_removed": removed,
                    },
                )

            logger.info(f"Removed {len(removed)} members from group {group_id} by {actor.sub}")
            return removed

        except Exception as e:
            logger.error(f"Failed to remove members from group {group_id}: {e}")
            raise

    async def update_member_role(
        self,
        db: AsyncSession,
        actor: User,
        group_id: int,
        user_id: str,
        old_role: str,
        new_role: str,
    ) -> None:
        """
        Update a member's role with audit logging.

        Args:
            db: Database session
            actor: Actor context with user_id (for FK) and sub (for audit)
            group_id: Group ID
            user_id: User ID
            old_role: Previous role
            new_role: New role
        """
        try:
            # Update role
            await db.execute(
                text("""
                    UPDATE user_group_members
                    SET group_role = :role
                    WHERE user_group_id = :group_id AND user_id = :user_id
                """),
                {"group_id": group_id, "user_id": user_id, "role": new_role},
            )

            # Log audit
            await self.audit_service.log_action(
                db=db,
                actor=actor,
                entity_type=self.entity_type,
                entity_id=str(group_id),
                action=AuditAction.UPDATE,
                changes={
                    "before": {"user_id": user_id, "role": old_role},
                    "after": {"user_id": user_id, "role": new_role},
                },
            )

            logger.info(f"Updated member {user_id} role to {new_role} in group {group_id} by {actor.id}")

        except Exception as e:
            logger.error(f"Failed to update member role in group {group_id}: {e}")
            raise
