"""User group service for managing groups and memberships."""

import asyncio
import logging
from collections import defaultdict
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..authorization import (
    SYSTEM_ROLE_CAPABILITIES,
    check_action_allowed,
)
from ..models.notification import NotificationData, NotificationType
from ..models.sub_agent import ActivationSource
from ..models.user_group import (
    BulkDeleteResult,
    MemberInfo,
    SubAgentRef,
    SubAgentRefWithStatus,
    UserGroup,
    UserGroupWithMembers,
)
from ..repositories.sub_agent_repository import SubAgentRepository
from ..repositories.user_group_repository import UserGroupRepository
from ..services.keycloak_admin_service import KeycloakAdminService
from ..services.notification_service import NotificationService
from ..services.sub_agent_service import SubAgentService

logger = logging.getLogger(__name__)


class UserGroupService:
    """Service for managing user groups and memberships."""

    def __init__(
        self,
        user_group_repository: UserGroupRepository | None = None,
        sub_agent_repository: SubAgentRepository | None = None,
        sub_agent_service: SubAgentService | None = None,
        notification_service: NotificationService | None = None,
        keycloak_admin_service: KeycloakAdminService | None = None,
    ):
        """Initialize user group service.

        Args:
            user_group_repository: Optional user group repository instance.
                If None, must be set via set_repository() before use.

            sub_agent_service: Optional sub-agent service for validation and queries.
                If None, must be set via set_sub_agent_service() before use.
            keycloak_admin_service: Optional Keycloak admin service for group sync.
                If None, Keycloak sync will be skipped.
        """
        self._repo = user_group_repository
        self._sub_agent_service = sub_agent_service
        self._notification_service = notification_service
        self._keycloak_service = keycloak_admin_service

    def set_repository(self, user_group_repository):
        """Set the user group repository (dependency injection)."""
        self._repo = user_group_repository

    def set_sub_agent_service(self, sub_agent_service: SubAgentService | None):
        """Set the sub-agent service (dependency injection)."""
        self._sub_agent_service = sub_agent_service

    def set_notification_service(self, notification_service: NotificationService | None):
        """Set the notification service (dependency injection)."""
        self._notification_service = notification_service

    def set_keycloak_service(self, keycloak_admin_service: KeycloakAdminService | None):
        """Set the Keycloak admin service (dependency injection)."""
        self._keycloak_service = keycloak_admin_service

    @property
    def repo(self) -> UserGroupRepository:
        """Get the user group repository, raising error if not set."""
        if self._repo is None:
            raise RuntimeError("UserGroupRepository not injected. Call set_repository() during initialization.")
        return self._repo

    @property
    def sub_agent_service(self) -> SubAgentService:
        """Get the sub-agent service, raising error if not set."""
        if self._sub_agent_service is None:
            raise RuntimeError("SubAgentService not injected. Call set_sub_agent_service() during initialization.")
        return self._sub_agent_service

    @property
    def notification_service(self) -> NotificationService:
        """Get the notification service, raising error if not set."""
        if self._notification_service is None:
            raise RuntimeError(
                "NotificationService not injected. Call set_notification_service() during initialization."
            )
        return self._notification_service

    @property
    def keycloak_service(self) -> KeycloakAdminService | None:
        """Get the Keycloak admin service (optional)."""
        return self._keycloak_service

    async def get_group(self, db: AsyncSession, group_id: int) -> UserGroup | None:
        """Get a group by ID.

        Args:
            db: Database session
            group_id: Group ID

        Returns:
            Group or None if not found
        """
        query = text("""
            SELECT id, name, description, keycloak_group_id, deleted_at, created_at, updated_at
            FROM user_groups
            WHERE id = :group_id AND deleted_at IS NULL
        """)

        try:
            result = await db.execute(query, {"group_id": group_id})
            row = result.mappings().first()

            if row is None:
                return None

            return UserGroup(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                keycloak_group_id=row["keycloak_group_id"],
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get group: {e}")
            raise

    async def get_group_with_members(self, db: AsyncSession, group_id: int) -> UserGroupWithMembers | None:
        """Get a group with its members.

        Args:
            db: Database session
            group_id: Group ID

        Returns:
            Group with members or None if not found
        """
        group = await self.get_group(db, group_id)
        if group is None:
            return None

        # Get member count and member info
        members_query = text("""
            SELECT u.id as user_id, u.email, u.first_name, u.last_name, ugm.group_role
            FROM user_group_members ugm
            JOIN users u ON u.id = ugm.user_id
            WHERE ugm.user_group_id = :group_id
            AND u.deleted_at IS NULL
            AND u.status = 'active'
        """)

        result = await db.execute(members_query, {"group_id": group_id})
        member_rows = result.mappings().all()

        members = [
            MemberInfo(
                user_id=row["user_id"],
                email=row["email"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                group_role=row["group_role"],
            )
            for row in member_rows
        ]

        return UserGroupWithMembers(
            **group.model_dump(),
            member_count=len(members),
            members=members,
        )

    async def list_groups(
        self,
        db: AsyncSession,
        page: int = 1,
        limit: int = 20,
        search: str | None = None,
    ) -> tuple[list[UserGroupWithMembers], int]:
        """List groups with pagination.

        Args:
            db: Database session
            page: Page number (1-indexed)
            limit: Items per page
            search: Search term for name

        Returns:
            Tuple of (groups with member counts, total count)
        """
        conditions = ["deleted_at IS NULL"]
        params: dict[str, Any] = {
            "limit": limit,
            "offset": (page - 1) * limit,
        }

        if search:
            conditions.append("name ILIKE :search")
            params["search"] = f"%{search}%"

        where_clause = "WHERE " + " AND ".join(conditions)

        count_query = text(f"""
            SELECT COUNT(*) as total FROM user_groups {where_clause}
        """)

        data_query = text(f"""
            SELECT id, name, description, keycloak_group_id, deleted_at, created_at, updated_at
            FROM user_groups
            {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)

        try:
            # Count query only needs search param if present
            count_params = {"search": params["search"]} if search else {}
            count_result = await db.execute(count_query, count_params)
            total = count_result.scalar() or 0

            result = await db.execute(data_query, params)
            rows = result.mappings().all()

            groups = []
            for row in rows:
                group = UserGroup(
                    id=row["id"],
                    name=row["name"],
                    description=row["description"],
                    keycloak_group_id=row["keycloak_group_id"],
                    deleted_at=row["deleted_at"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                group_with_members = await self.get_group_with_members(db, group.id)
                if group_with_members:
                    groups.append(group_with_members)

            return groups, total
        except Exception as e:
            logger.error(f"Failed to list groups: {e}")
            raise

    async def list_user_groups(
        self,
        db: AsyncSession,
        user_id: str,
    ) -> list[UserGroupWithMembers]:
        """List groups where a user is a member.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            List of groups the user belongs to
        """
        query = text("""
            SELECT ug.id, ug.name, ug.description, ug.keycloak_group_id,
                   ug.deleted_at, ug.created_at, ug.updated_at
            FROM user_groups ug
            JOIN user_group_members ugm ON ugm.user_group_id = ug.id
            WHERE ugm.user_id = :user_id
            AND ug.deleted_at IS NULL
            ORDER BY ug.name
        """)

        try:
            result = await db.execute(query, {"user_id": user_id})
            rows = result.mappings().all()

            groups = []
            for row in rows:
                group_with_members = await self.get_group_with_members(db, row["id"])
                if group_with_members:
                    groups.append(group_with_members)

            return groups
        except Exception as e:
            logger.error(f"Failed to list user groups: {e}")
            raise

    async def get_user_group_memberships(
        self,
        db: AsyncSession,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """Get user's group memberships with their roles.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            List of dicts with group id, name, and the user's role in that group
        """
        query = text("""
            SELECT ug.id, ug.name, ugm.group_role
            FROM user_groups ug
            JOIN user_group_members ugm ON ugm.user_group_id = ug.id
            WHERE ugm.user_id = :user_id
            AND ug.deleted_at IS NULL
            ORDER BY ug.name
        """)

        try:
            result = await db.execute(query, {"user_id": user_id})
            rows = result.mappings().all()

            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "group_role": row["group_role"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to get user group memberships: {e}")
            raise

    async def create_group(
        self,
        db: AsyncSession,
        actor_sub: str,
        name: str,
        description: str | None = None,
    ) -> UserGroup:
        """Create a new group.

        Args:
            db: Database session
            actor_sub: The sub of the user creating the group
            name: Group name
            description: Group description

        Returns:
            Created group
        """
        try:
            # First, create group in database (within transaction, not yet committed)
            group_id = await self.repo.create(
                db=db,
                actor_sub=actor_sub,
                fields={
                    "name": name,
                    "description": description,
                },
                returning="id",
            )

            # Then sync to Keycloak if service is configured
            # If this fails, exception will cause DB transaction to rollback
            keycloak_group_id = None
            if self.keycloak_service:
                try:
                    keycloak_group_id = await self.keycloak_service.create_group(name, description)
                    await self.repo.update(
                        db=db,
                        actor_sub=actor_sub,
                        entity_id=group_id,
                        fields={"keycloak_group_id": keycloak_group_id},
                        fetch_before=False,
                    )
                    logger.info(f"Synced group '{name}' to Keycloak (ID: {keycloak_group_id})")
                except Exception as e:
                    logger.error(f"Failed to sync group to Keycloak: {e}")
                    # Re-raise to trigger DB transaction rollback
                    raise

            # Fetch and return the created group
            group = await self.get_group(db, group_id)
            if not group:
                raise RuntimeError("Failed to retrieve created group")

            logger.info(f"Created group: {name}")
            return group
        except Exception as e:
            logger.error(f"Failed to create group: {e}")
            raise

    async def update_group(
        self,
        db: AsyncSession,
        actor_sub: str,
        group_id: int,
        name: str | None = None,
        description: str | None = None,
    ) -> UserGroup | None:
        """Update a group.

        Args:
            db: Database session
            actor_sub: The sub of the user updating the group
            group_id: Group ID
            name: New name (optional)
            description: New description (optional)

        Returns:
            Updated group or None if not found
        """
        try:
            # Check if group exists first
            existing = await self.get_group(db, group_id)
            if existing is None:
                return None

            # Build update fields
            fields: dict[str, Any] = {}
            if name is not None:
                fields["name"] = name
            if description is not None:
                fields["description"] = description

            if not fields:
                # No fields to update, just return current group
                return existing

            # Update with automatic audit (returns None)
            await self.repo.update(
                db=db,
                actor_sub=actor_sub,
                entity_id=group_id,
                fields=fields,
                fetch_before=True,
            )

            if self.keycloak_service:
                keycloak_group_id = existing.keycloak_group_id

                if keycloak_group_id:
                    try:
                        # Use existing name/description if not provided
                        update_name = name if name is not None else existing.name
                        update_description = description if description is not None else existing.description
                        await self.keycloak_service.update_group(keycloak_group_id, update_name, update_description)
                        logger.info(f"Synced group update to Keycloak (ID: {keycloak_group_id})")
                    except Exception as e:
                        logger.error(f"Failed to sync group update to Keycloak: {e}")
                        raise
                else:
                    logger.warning(f"Group {group_id} has no Keycloak ID; skipping Keycloak update")

            # Fetch and return updated group
            group = await self.get_group(db, group_id)
            logger.info(f"Updated group: {group_id}")
            return group
        except Exception as e:
            logger.error(f"Failed to update group: {e}")
            raise

    async def delete_group(self, db: AsyncSession, actor_sub: str, group_id: int, force: bool = False) -> bool:
        """Delete a group (soft delete).

        Args:
            db: Database session
            actor_sub: The sub of the user deleting the group
            group_id: Group ID
            force: If True, delete even if sub-agents are assigned

        Returns:
            True if deleted, False if not found or blocked
        """
        # Check if group exists
        existing = await self.get_group(db, group_id)
        if existing is None:
            return False

        # Check if sub-agents are assigned
        if not force:
            check_query = text("""
                SELECT COUNT(*) as count
                FROM sub_agent_permissions
                WHERE user_group_id = :group_id
            """)
            result = await db.execute(check_query, {"group_id": group_id})
            count = result.scalar() or 0

            if count > 0:
                raise ValueError(
                    f"Cannot delete group: {count} sub-agents are assigned. Use force=true to delete anyway."
                )

        try:
            # Get Keycloak group ID before soft delete
            keycloak_group_id = existing.keycloak_group_id
            if self.keycloak_service:
                # Delete from Keycloak first (fail-fast)
                if keycloak_group_id:
                    try:
                        await self.keycloak_service.delete_group(keycloak_group_id)
                        logger.info(f"Deleted Keycloak group (ID: {keycloak_group_id})")
                    except Exception as e:
                        logger.error(f"Failed to delete Keycloak group: {e}")
                        raise
                else:
                    logger.warning(f"Group {group_id} has no Keycloak ID; skipping Keycloak deletion")

            # Soft delete with automatic audit (returns None)
            await self.repo.delete(
                db=db,
                actor_sub=actor_sub,
                entity_id=group_id,
                soft=True,
            )

            logger.info(f"Deleted group: {group_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete group: {e}")
            raise

    async def bulk_delete_groups(
        self, db: AsyncSession, actor_sub: str, group_ids: list[int], force: bool = False
    ) -> list[BulkDeleteResult]:
        """Bulk delete groups.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the deletions
            group_ids: List of group IDs
            force: If True, delete even if sub-agents are assigned

        Returns:
            List of deletion results
        """
        results = []

        for group_id in group_ids:
            try:
                success = await self.delete_group(db, actor_sub, group_id, force)
                if success:
                    results.append(BulkDeleteResult(group_id=group_id, success=True))
                else:
                    results.append(BulkDeleteResult(group_id=group_id, success=False, error="Group not found"))
            except ValueError as e:
                results.append(BulkDeleteResult(group_id=group_id, success=False, error=str(e)))
            except Exception as e:
                results.append(BulkDeleteResult(group_id=group_id, success=False, error=str(e)))

        return results

    # Member management

    async def _add_members(
        self,
        db: AsyncSession,
        actor_sub: str,
        group_id: int,
        user_ids: list[str],
        role: Literal["read", "write", "manager"] = "read",
    ) -> list[MemberInfo]:
        """Internal helper to add members to a group.

        This method contains the core logic for adding members and can be used
        by both bulk and single-member operations.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            group_id: Group ID
            user_ids: List of user IDs to add
            role: Role for the new members

        Returns:
            List of added members
        """
        existing = await self.get_group(db, group_id)
        if existing is None:
            raise ValueError("Group not found")

        # Prepare member additions
        member_additions = [{"user_id": user_id, "role": role} for user_id in user_ids]

        # First, add members to database (within transaction, not yet committed)
        await self.repo.add_members(
            db=db,
            actor_sub=actor_sub,
            group_id=group_id,
            member_additions=member_additions,
        )

        # Then sync to Keycloak sequentially
        # If this fails, exception will cause DB transaction to rollback
        if self.keycloak_service:
            if existing.keycloak_group_id:
                tasks = []
                # TODO: this is not 100% transaction-safe if adding multiple users and one fails
                #       we could end up with partial additions in Keycloak vs DB
                try:
                    for user_id in user_ids:
                        user_sub = user_id  # in this context, user_id is the Keycloak user ID (sub)
                        tasks.append(self.keycloak_service.add_user_to_group(user_sub, existing.keycloak_group_id))
                        logger.info(f"Added user {user_id} to Keycloak group {existing.keycloak_group_id}")
                    await asyncio.gather(*tasks)
                except Exception as e:
                    logger.error(f"Failed to add users to Keycloak group: {e}")
                    # Re-raise to trigger DB transaction rollback
                    raise
            else:
                logger.warning(f"Group {group_id} has no Keycloak ID; skipping Keycloak member additions")

        logger.info(f"Added {len(user_ids)} members to group {group_id}")

        # Auto-activate group default agents for new members (only approved agents)
        try:
            # Get default agents with approval status using internal helper
            rows = await self._get_group_default_agents_base(db, group_id)
            if rows:
                # Filter to only approved agents (those that can be activated)
                approved_agents = [
                    (agent_id, agent_name) for agent_id, agent_name, status in rows if status == "approved"
                ]

                # Bulk activate each approved agent for all new users
                for agent_id, agent_name in approved_agents:
                    await self.sub_agent_service.repo.bulk_activate_sub_agent(
                        db=db,
                        actor_sub="SYSTEM",
                        user_ids=user_ids,
                        sub_agent_id=agent_id,
                        activated_by=ActivationSource.GROUP,
                        group_id=group_id,
                    )

                # Bulk create notifications for users (only for activated agents)
                if len(approved_agents) > 0:
                    agent_names = [name for _, name in approved_agents[:3]]
                    if len(approved_agents) > 3:
                        agent_names.append(f"and {len(approved_agents) - 3} more")

                    notifications = [
                        NotificationData(
                            user_id=user_id,
                            notification_type=NotificationType.AGENT_ACTIVATED,
                            title=f"Agents activated from {existing.name}",
                            message=f"{len(approved_agents)} agent(s) auto-enabled: {', '.join(agent_names)}",
                            metadata={
                                "group_id": group_id,
                                "group_name": existing.name,
                                "sub_agent_ids": [agent_id for agent_id, _ in approved_agents],
                                "count": len(approved_agents),
                            },
                        )
                        for user_id in user_ids
                    ]
                    await self.notification_service.bulk_create_notifications(db, notifications)

                pending_count = len(rows) - len(approved_agents)
                logger.info(
                    f"Auto-activated {len(approved_agents)} approved default agents for {len(user_ids)} new members "
                    f"of group {group_id} ({pending_count} non-approved agents will activate once approved)"
                )
        except Exception as e:
            logger.error(f"Failed to auto-activate default agents: {e}")
            # Don't raise - member addition succeeded, activation failure is non-critical

        # Fetch and return added members
        member_query = text("""
            SELECT u.id as user_id, u.email, u.first_name, u.last_name, ugm.group_role
            FROM user_group_members ugm
            JOIN users u ON u.id = ugm.user_id
            WHERE ugm.user_group_id = :group_id AND ugm.user_id = ANY(:user_ids)
        """)
        member_result = await db.execute(member_query, {"group_id": group_id, "user_ids": user_ids})
        rows = member_result.mappings().all()

        return [
            MemberInfo(
                user_id=row["user_id"],
                email=row["email"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                group_role=row["group_role"],
            )
            for row in rows
        ]

    async def list_members(
        self,
        db: AsyncSession,
        group_id: int,
        page: int = 1,
        limit: int = 20,
    ) -> tuple[list[MemberInfo], int]:
        """List members of a group.

        Args:
            db: Database session
            group_id: Group ID
            page: Page number
            limit: Items per page

        Returns:
            Tuple of (members, total count)
        """
        count_query = text("""
            SELECT COUNT(*) as total
            FROM user_group_members ugm
            JOIN users u ON u.id = ugm.user_id
            WHERE ugm.user_group_id = :group_id
            AND u.deleted_at IS NULL
            AND u.status = 'active'
        """)

        data_query = text("""
            SELECT u.id as user_id, u.email, u.first_name, u.last_name, ugm.group_role
            FROM user_group_members ugm
            JOIN users u ON u.id = ugm.user_id
            WHERE ugm.user_group_id = :group_id
            AND u.deleted_at IS NULL
            AND u.status = 'active'
            ORDER BY u.first_name, u.last_name
            LIMIT :limit OFFSET :offset
        """)

        params = {
            "group_id": group_id,
            "limit": limit,
            "offset": (page - 1) * limit,
        }

        try:
            count_result = await db.execute(count_query, params)
            total = count_result.scalar() or 0

            result = await db.execute(data_query, params)
            rows = result.mappings().all()

            members = [
                MemberInfo(
                    user_id=row["user_id"],
                    email=row["email"],
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    group_role=row["group_role"],
                )
                for row in rows
            ]

            return members, total
        except Exception as e:
            logger.error(f"Failed to list members: {e}")
            raise

    async def add_members(
        self,
        db: AsyncSession,
        actor_sub: str,
        group_id: int,
        user_ids: list[str],
        role: Literal["read", "write", "manager"] = "read",
    ) -> list[MemberInfo]:
        """Add members to a group (bulk operation).

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            group_id: Group ID
            user_ids: List of user IDs to add
            role: Role for the new members

        Returns:
            Updated member list
        """
        try:
            # Use internal helper
            await self._add_members(db, actor_sub, group_id, user_ids, role)

            # Return updated member list
            members, _ = await self.list_members(db, group_id, page=1, limit=1000)
            return members
        except Exception as e:
            logger.error(f"Failed to add members: {e}")
            raise

    async def add_member(
        self,
        db: AsyncSession,
        actor_sub: str,
        group_id: int,
        user_id: str,
        role: Literal["read", "write", "manager"] = "read",
    ) -> MemberInfo:
        """Add a single member to a group.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            group_id: Group ID
            user_id: User ID to add
            role: Role for the new member

        Returns:
            Added member info
        """
        try:
            # Use internal helper
            added = await self._add_members(db, actor_sub, group_id, [user_id], role)
            if not added:
                raise ValueError("Failed to add member")
            return added[0]
        except Exception as e:
            logger.error(f"Failed to add member: {e}")
            raise

    async def update_member_role(
        self,
        db: AsyncSession,
        actor_sub: str,
        group_id: int,
        user_id: str,
        role: Literal["read", "write", "manager"],
    ) -> MemberInfo | None:
        """Update a member's role.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            group_id: Group ID
            user_id: User ID
            role: New role

        Returns:
            Updated member info or None if not found
        """
        try:
            # Get current role for audit
            current_role_query = text("""
                SELECT group_role FROM user_group_members
                WHERE user_group_id = :group_id AND user_id = :user_id
            """)
            result = await db.execute(current_role_query, {"group_id": group_id, "user_id": user_id})
            current_row = result.mappings().first()

            if current_row is None:
                return None

            old_role = current_row["group_role"]

            # Update role with automatic audit
            await self.repo.update_member_role(
                db=db,
                actor_sub=actor_sub,
                group_id=group_id,
                user_id=user_id,
                old_role=old_role,
                new_role=role,
            )

            # Fetch updated member info
            member_query = text("""
                SELECT u.id as user_id, u.email, u.first_name, u.last_name, ugm.group_role
                FROM user_group_members ugm
                JOIN users u ON u.id = ugm.user_id
                WHERE ugm.user_group_id = :group_id AND ugm.user_id = :user_id
            """)
            member_result = await db.execute(member_query, {"group_id": group_id, "user_id": user_id})
            member_row = member_result.mappings().first()

            if member_row is None:
                return None

            logger.info(f"Updated member {user_id} role to {role} in group {group_id}")

            # Notify user about role change
            try:
                # Get group name for notification
                group_query = text("SELECT name FROM user_groups WHERE id = :group_id")
                group_result = await db.execute(group_query, {"group_id": group_id})
                group_row = group_result.mappings().first()
                group_name = group_row["name"] if group_row else f"Group {group_id}"

                notification = NotificationData(
                    user_id=user_id,
                    notification_type=NotificationType.ROLE_UPDATED,
                    title=f"Role changed in {group_name}",
                    message=f"Your role in '{group_name}' has been changed from '{old_role}' to '{role}'.",
                    metadata={"group_id": group_id, "old_role": old_role, "new_role": role},
                )
                await self.notification_service.bulk_create_notifications(db, [notification])
            except Exception as e:
                logger.error(f"Failed to create role update notification: {e}")
                # Don't fail the operation if notification fails

            return MemberInfo(
                user_id=member_row["user_id"],
                email=member_row["email"],
                first_name=member_row["first_name"],
                last_name=member_row["last_name"],
                group_role=member_row["group_role"],
            )
        except Exception as e:
            logger.error(f"Failed to update member role: {e}")
            raise

    async def _remove_members(
        self,
        db: AsyncSession,
        actor_sub: str,
        group_id: int,
        user_ids: list[str],
    ) -> list[str]:
        """Internal helper to remove members from a group.

        This method contains the core logic for removing members and can be used
        by both bulk and single-member operations.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            group_id: Group ID
            user_ids: List of user IDs to remove

        Returns:
            List of user IDs that were actually removed
        """
        existing = await self.get_group(db, group_id)
        if existing is None:
            raise ValueError("Group not found")

        # Check manager count before starting removals
        admin_count_query = text("""
            SELECT COUNT(*) as count
            FROM user_group_members
            WHERE user_group_id = :group_id AND group_role = 'manager'
        """)
        count_result = await db.execute(admin_count_query, {"group_id": group_id})
        manager_count = count_result.scalar() or 0

        # Count how many managers we're trying to remove
        check_roles_query = text("""
            SELECT user_id FROM user_group_members
            WHERE user_group_id = :group_id
            AND user_id = ANY(:user_ids)
            AND group_role = 'manager'
        """)
        roles_result = await db.execute(check_roles_query, {"group_id": group_id, "user_ids": user_ids})
        managers_to_remove = [row[0] for row in roles_result.fetchall()]

        # Check if we're removing all managers
        if managers_to_remove and len(managers_to_remove) >= manager_count:
            raise ValueError("Cannot remove all managers from a group")

        # Remove from database first (within transaction, not yet committed)
        # Repository returns which members were actually removed
        removed = await self.repo.remove_members(
            db=db,
            actor_sub=actor_sub,
            group_id=group_id,
            user_ids=user_ids,
        )

        # Then sync to Keycloak sequentially
        # If this fails, exception will cause DB transaction to rollback
        if self.keycloak_service:
            if existing.keycloak_group_id:
                tasks = []
                for user_id in removed:
                    tasks.append(self.keycloak_service.remove_user_from_group(user_id, existing.keycloak_group_id))
                    logger.info(f"Removed user {user_id} from Keycloak group {existing.keycloak_group_id}")
                await asyncio.gather(*tasks)
            else:
                logger.warning(f"Group {group_id} has no Keycloak ID; skipping Keycloak member removals")

        logger.info(f"Removed {len(removed)} members from group {group_id}")

        # Notify removed members
        try:
            if removed:
                notifications = [
                    NotificationData(
                        user_id=user_id,
                        notification_type=NotificationType.GROUP_REMOVED,
                        title=f"Removed from {existing.name}",
                        message=f"You have been removed from the group '{existing.name}'.",
                        metadata={"group_id": group_id, "group_name": existing.name},
                    )
                    for user_id in removed
                ]
                await self.notification_service.bulk_create_notifications(db, notifications)
        except Exception as e:
            logger.error(f"Failed to create removal notifications: {e}")
            # Don't fail the operation

        # Handle activation cleanup for removed members
        try:
            # Get all activations for removed users from this group
            query = text("""
                SELECT user_id, sub_agent_id
                FROM user_sub_agent_activations
                WHERE user_id = ANY(:user_ids)
                AND activated_by = 'group'
                AND activated_by_groups ? :group_id_text
            """)

            result = await db.execute(
                query,
                {"user_ids": removed, "group_id_text": str(group_id)},
            )
            activations = result.fetchall()

            # Group by sub_agent_id for bulk operations
            activations_by_agent = defaultdict(list)
            for user_id, sub_agent_id in activations:
                activations_by_agent[sub_agent_id].append(user_id)

            # Bulk deactivate per agent
            for sub_agent_id, user_ids in activations_by_agent.items():
                await self.sub_agent_service.repo.bulk_deactivate_sub_agent(
                    db=db,
                    actor_sub="SYSTEM",
                    user_ids=user_ids,
                    sub_agent_id=sub_agent_id,
                    group_id=group_id,
                )

            if activations:
                logger.info(f"Removed group {group_id} from {len(activations)} activations for {len(removed)} users")
        except Exception as e:
            logger.error(f"Failed to cleanup activations on member removal: {e}")
            # Don't raise - member removal succeeded, cleanup failure is logged

        return removed

    async def remove_members(
        self,
        db: AsyncSession,
        actor_sub: str,
        group_id: int,
        user_ids: list[str],
    ) -> list[MemberInfo]:
        """Remove multiple members from a group (bulk operation).

        Cannot remove the last manager.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            group_id: Group ID
            user_ids: List of user IDs to remove

        Returns:
            Updated list of members
        """
        try:
            # Use internal helper
            await self._remove_members(db, actor_sub, group_id, user_ids)

            # Return updated member list
            members, _ = await self.list_members(db, group_id, page=1, limit=1000)
            return members
        except Exception as e:
            logger.error(f"Failed to remove members: {e}")
            raise

    async def remove_member(
        self,
        db: AsyncSession,
        actor_sub: str,
        group_id: int,
        user_id: str,
    ) -> list[MemberInfo]:
        """Remove a single member from a group.

        Cannot remove the last manager.

        Args:
            db: Database session
            actor_sub: The sub of the user performing the action
            group_id: Group ID
            user_id: User ID to remove

        Returns:
            Updated member list
        """
        try:
            # Use internal helper
            removed = await self._remove_members(db, actor_sub, group_id, [user_id])
            if not removed:
                raise ValueError("Failed to remove member")

            # Return updated member list
            members, _ = await self.list_members(db, group_id, page=1, limit=1000)
            if not members:
                raise ValueError("No members found after removal")
            return members
        except Exception as e:
            logger.error(f"Failed to remove member: {e}")
            raise

    async def is_group_manager(self, db: AsyncSession, group_id: int, user_id: str) -> bool:
        """Check if a user is a manager of a group.

        Args:
            db: Database session
            group_id: Group ID
            user_id: User ID

        Returns:
            True if user is group manager
        """
        query = text("""
            SELECT 1 FROM user_group_members
            WHERE user_group_id = :group_id
            AND user_id = :user_id
            AND group_role = 'manager'
        """)

        try:
            result = await db.execute(query, {"group_id": group_id, "user_id": user_id})
            return result.first() is not None
        except Exception as e:
            logger.error(f"Failed to check group admin: {e}")
            return False

    async def is_group_member(self, db: AsyncSession, group_id: int, user_id: str) -> bool:
        """Check if a user is a member of a group.

        Args:
            db: Database session
            group_id: Group ID
            user_id: User ID

        Returns:
            True if user is a group member
        """
        query = text("""
            SELECT 1 FROM user_group_members
            WHERE user_group_id = :group_id AND user_id = :user_id
        """)

        try:
            result = await db.execute(query, {"group_id": group_id, "user_id": user_id})
            return result.first() is not None
        except Exception as e:
            logger.error(f"Failed to check group membership: {e}")
            return False

    async def is_group_admin(self, db: AsyncSession, group_id: int, user_id: str) -> bool:
        """Backward compatibility alias for is_group_manager."""
        return await self.is_group_manager(db, group_id, user_id)

    async def check_user_permission(self, db: AsyncSession, user_id: str, resource: str, action: str) -> bool:
        """Check if a user has a specific permission based on their system role.

        Args:
            db: Database session
            user_id: User ID
            resource: Resource name (e.g., 'sub_agents', 'users', 'groups')
            action: Action name (e.g., 'read', 'write', 'approve', 'delete')

        Returns:
            True if user's system role grants the permission
        """
        query = text("""
            SELECT role FROM users WHERE id = :user_id
        """)

        try:
            result = await db.execute(query, {"user_id": user_id})
            row = result.first()

            if row is None:
                return False

            user_role = row[0]

            # Check if role has the capability
            if user_role not in SYSTEM_ROLE_CAPABILITIES:
                return False

            role_perms = SYSTEM_ROLE_CAPABILITIES[user_role]
            if resource not in role_perms:
                return False

            return action in role_perms[resource]

        except Exception as e:
            logger.error(f"Failed to check user permission: {e}")
            return False

    async def check_resource_permission(
        self, db: AsyncSession, user_id: str, resource_type: str, resource_id: int, action: str
    ) -> bool:
        """Check if a user has permission to perform an action on a specific resource.

        Authorization model: effective_permissions = resource_permissions ∩ role_capabilities

        This combines:
        1. System role capabilities (for special actions like 'approve')
        2. Group-based resource permissions (what the group can do on the resource)
        3. User's role within the group (what actions their role allows)

        Args:
            db: Database session
            user_id: User ID
            resource_type: Type of resource (e.g., 'sub_agents')
            resource_id: ID of the specific resource
            action: Action to perform (e.g., 'read', 'write', 'approve')

        Returns:
            True if user has permission

        Permission Logic:
            - Owner bypass: Resource owners always have full access
            - Public bypass: Public resources are readable by all (read only)
            - For 'approve' action: Requires system role 'approver' or 'admin'
              AND group role 'write' or 'manager' for a group with access to the resource
            - For other actions: User's group must have permission on resource
              AND user's role in that group must allow the action
        """
        try:
            # For sub_agents, check ownership first
            if resource_type == "sub_agents":
                owner_query = text("SELECT owner_user_id, is_public FROM sub_agents WHERE id = :resource_id")
                owner_result = await db.execute(owner_query, {"resource_id": resource_id})
                owner_row = owner_result.first()

                if owner_row:
                    # Owner has full access
                    if owner_row[0] == user_id:
                        return True

                    # Public sub-agents allow read access
                    if owner_row[1] and action == "read":
                        return True

            # Special handling for approve action
            if action == "approve":
                # Check if system role has approve capability
                if not await self.check_user_permission(db, user_id, resource_type, "approve"):
                    return False

                # Must have write or manager group role with access to the resource
                # Check both: (1) group has permissions on resource, (2) user role allows approval
                group_query = text("""
                    SELECT ugm.group_role, sap.permissions
                    FROM user_group_members ugm
                    JOIN sub_agent_permissions sap ON ugm.user_group_id = sap.user_group_id
                    WHERE ugm.user_id = :user_id 
                      AND sap.sub_agent_id = :resource_id
                """)
                group_result = await db.execute(group_query, {"user_id": user_id, "resource_id": resource_id})

                for row in group_result:
                    user_group_role = row[0]
                    resource_permissions = row[1]

                    # User must have write or manager role (roles that can approve)
                    if user_group_role in ("write", "manager"):
                        # Group must have write permission on the resource
                        if "write" in resource_permissions:
                            return True

                return False  # For other actions, check intersection of resource permissions and role capabilities
            if resource_type == "sub_agents":
                group_query = text("""
                    SELECT ugm.group_role, sap.permissions
                    FROM user_group_members ugm
                    JOIN sub_agent_permissions sap ON ugm.user_group_id = sap.user_group_id
                    WHERE ugm.user_id = :user_id AND sap.sub_agent_id = :resource_id
                """)
                group_result = await db.execute(group_query, {"user_id": user_id, "resource_id": resource_id})

                for row in group_result:
                    user_group_role = row[0]
                    resource_permissions = row[1]  # PostgreSQL array from sub_agent_permissions

                    # Check if resource has the required permission
                    if action not in resource_permissions:
                        continue

                    # Check if user's role allows this action (intersection)
                    if check_action_allowed(user_group_role, resource_type, action):
                        return True

            return False

        except Exception as e:
            logger.error(f"Failed to check resource permission: {e}")
            return False

    async def _get_group_default_agents_base(
        self,
        db: AsyncSession,
        group_id: int,
    ) -> list[tuple[int, str, str]]:
        """
        Internal helper to get basic default agent info for a group.

        Args:
            db: Database session
            group_id: Group ID

        Returns:
            List of tuples: (agent_id, agent_name, approval_status)
        """
        query = text("""
            SELECT 
                sa.id, 
                sa.name,
                COALESCE(cv_default.status, cv_current.status, 'draft') as approval_status
            FROM user_group_default_agents ugda
            JOIN sub_agents sa ON ugda.sub_agent_id = sa.id
            LEFT JOIN sub_agent_config_versions cv_default
                ON sa.id = cv_default.sub_agent_id AND sa.default_version = cv_default.version
            LEFT JOIN sub_agent_config_versions cv_current
                ON sa.id = cv_current.sub_agent_id AND sa.current_version = cv_current.version
            WHERE ugda.user_group_id = :group_id
            AND sa.deleted_at IS NULL
            ORDER BY ugda.created_at DESC
        """)

        result = await db.execute(query, {"group_id": group_id})
        rows = result.fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    async def get_group_default_agents(
        self,
        db: AsyncSession,
        group_id: int,
    ) -> list[SubAgentRef]:
        """
        Get basic default agents for a group (without user-specific activation status).

        Args:
            db: Database session
            group_id: Group ID

        Returns:
            List of sub-agents with basic info
        """
        try:
            rows = await self._get_group_default_agents_base(db, group_id)
            return [
                SubAgentRef(
                    id=agent_id,
                    name=agent_name,
                )
                for agent_id, agent_name, _ in rows
            ]
        except Exception as e:
            logger.error(f"Failed to get default agents for group {group_id}: {e}")
            raise

    async def get_group_accessible_agents(
        self,
        db: AsyncSession,
        group_id: int,
        user_id: str,
    ) -> list[SubAgentRefWithStatus]:
        """
        Get all accessible approved agents for a group with default flags and status indicators.

        This method returns ALL approved agents that the group has permission to access,
        with a flag indicating which ones are set as defaults for automatic activation.

        Args:
            db: Database session
            group_id: Group ID
            user_id: User ID to check activation status

        Returns:
            List of accessible approved sub-agents with default flag, approval status, and activation status
        """
        query = text("""
            SELECT 
                sa.id, 
                sa.name,
                COALESCE(cv_default.status, cv_current.status, 'draft') as approval_status,
                (ugda.sub_agent_id IS NOT NULL) as is_default,
                (uaa.user_id IS NOT NULL) as is_activated,
                uaa.activated_by_groups
            FROM sub_agent_permissions sap
            JOIN sub_agents sa ON sap.sub_agent_id = sa.id
            LEFT JOIN user_group_default_agents ugda 
                ON sa.id = ugda.sub_agent_id AND ugda.user_group_id = :group_id
            LEFT JOIN sub_agent_config_versions cv_default
                ON sa.id = cv_default.sub_agent_id AND sa.default_version = cv_default.version
            LEFT JOIN sub_agent_config_versions cv_current
                ON sa.id = cv_current.sub_agent_id AND sa.current_version = cv_current.version
            LEFT JOIN user_sub_agent_activations uaa
                ON sa.id = uaa.sub_agent_id AND uaa.user_id = :user_id
            WHERE sap.user_group_id = :group_id
                AND sa.deleted_at IS NULL
                AND sa.default_version IS NOT NULL
            ORDER BY is_default DESC, sa.name ASC
        """)

        try:
            result = await db.execute(query, {"group_id": group_id, "user_id": user_id})
            rows = result.mappings().all()

            return [
                SubAgentRefWithStatus(
                    id=row["id"],
                    name=row["name"],
                    approval_status=row["approval_status"],
                    is_activated=row["is_activated"],
                    activated_by_groups=row["activated_by_groups"],
                    is_default=row["is_default"],
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to get accessible agents with defaults for group {group_id}: {e}")
            raise

    async def _activate_default_agent(
        self,
        db: AsyncSession,
        group_id: int,
        sub_agent_id: int,
        actor_sub: str,
    ) -> None:
        """Internal helper to activate a default agent for all group members.

        This method contains the core logic for activating an agent and can be used
        by both bulk and single-agent operations.

        IMPORTANT: Only activates agents that are approved (default_version IS NOT NULL).
        Non-approved agents remain inactive until approved.

        Args:
            db: Database session
            group_id: Group ID
            sub_agent_id: Sub-agent ID to activate
            actor_sub: User performing the action
        """
        # Check if agent is approved (has default_version)
        approval_check_query = text("""
            SELECT default_version FROM sub_agents
            WHERE id = :sub_agent_id AND deleted_at IS NULL
        """)
        approval_result = await db.execute(approval_check_query, {"sub_agent_id": sub_agent_id})
        approval_row = approval_result.first()

        if not approval_row or approval_row[0] is None:
            # Agent is not approved yet - skip activation
            logger.info(f"Skipping activation of non-approved agent {sub_agent_id} for group {group_id}")
            return

        # Get all current members
        members_query = text("""
            SELECT user_id FROM user_group_members
            WHERE user_group_id = :group_id
        """)
        members_result = await db.execute(members_query, {"group_id": group_id})
        member_user_ids = [row[0] for row in members_result.fetchall()]

        if not member_user_ids:
            return

        # Get group name for notifications
        group_query = text("SELECT name FROM user_groups WHERE id = :group_id")
        group_result = await db.execute(group_query, {"group_id": group_id})
        group_name = group_result.scalar() or f"Group {group_id}"

        # Get agent name for notifications
        agent_names = await self.sub_agent_service.get_agent_names(db, [sub_agent_id])
        agent_name = agent_names.get(sub_agent_id, f"Agent {sub_agent_id}")

        # Bulk activate the agent
        await self.sub_agent_service.repo.bulk_activate_sub_agent(
            db=db,
            actor_sub=actor_sub,
            user_ids=member_user_ids,
            sub_agent_id=sub_agent_id,
            activated_by=ActivationSource.GROUP,
            group_id=group_id,
        )

        # Bulk create notifications
        notifications = [
            NotificationData(
                user_id=user_id,
                notification_type=NotificationType.AGENT_ACTIVATED,
                title=f"Agent '{agent_name}' enabled",
                message=f"The agent '{agent_name}' has been automatically enabled because it was added to default agents for the group '{group_name}'.",
                metadata={"sub_agent_id": sub_agent_id, "group_id": group_id},
            )
            for user_id in member_user_ids
        ]
        await self.notification_service.bulk_create_notifications(db, notifications)

        logger.info(f"Activated agent {sub_agent_id} for {len(member_user_ids)} members of group {group_id}")

    async def _deactivate_default_agent(
        self,
        db: AsyncSession,
        group_id: int,
        sub_agent_id: int,
        actor_sub: str,
    ) -> None:
        """Internal helper to deactivate a default agent for all group members.

        This method contains the core logic for deactivating an agent and can be used
        by both bulk and single-agent operations.

        Args:
            db: Database session
            group_id: Group ID
            sub_agent_id: Sub-agent ID to deactivate
            actor_sub: User performing the action
        """
        # Get all current members
        members_query = text("""
            SELECT user_id FROM user_group_members
            WHERE user_group_id = :group_id
        """)
        members_result = await db.execute(members_query, {"group_id": group_id})
        member_user_ids = [row[0] for row in members_result.fetchall()]

        if not member_user_ids:
            return

        # Get group name for notifications
        group_query = text("SELECT name FROM user_groups WHERE id = :group_id")
        group_result = await db.execute(group_query, {"group_id": group_id})
        group_name = group_result.scalar() or f"Group {group_id}"

        # Get agent name for notifications
        agent_names = await self.sub_agent_service.get_agent_names(db, [sub_agent_id])
        agent_name = agent_names.get(sub_agent_id, f"Agent {sub_agent_id}")

        # Bulk deactivate the agent
        await self.sub_agent_service.repo.bulk_deactivate_sub_agent(
            db=db,
            actor_sub=actor_sub,
            user_ids=member_user_ids,
            sub_agent_id=sub_agent_id,
            group_id=group_id,
        )

        # Notify users about deactivation
        notifications = [
            NotificationData(
                user_id=user_id,
                notification_type=NotificationType.AGENT_DEACTIVATED,
                title=f"Agent '{agent_name}' disabled",
                message=f"The agent '{agent_name}' has been automatically disabled because it was removed from default agents for the group '{group_name}'.",
                metadata={"sub_agent_id": sub_agent_id, "group_id": group_id},
            )
            for user_id in member_user_ids
        ]
        await self.notification_service.bulk_create_notifications(db, notifications)

        logger.info(f"Deactivated agent {sub_agent_id} for {len(member_user_ids)} members of group {group_id}")

    async def set_group_default_agents(
        self,
        db: AsyncSession,
        group_id: int,
        sub_agent_ids: list[int],
        actor_sub: str,
    ) -> None:
        """
        Set (replace) default agents for a group (bulk operation).

        Validates that all sub-agents are approved and group has permissions.
        Also activates new defaults and deactivates removed defaults for all existing members.

        Args:
            db: Database session
            group_id: Group ID
            sub_agent_ids: List of sub-agent IDs to set as defaults
            actor_sub: User performing the action
        """
        try:
            # Get current defaults to calculate diff
            current_defaults_query = text("""
                SELECT sub_agent_id FROM user_group_default_agents
                WHERE user_group_id = :group_id
            """)
            current_result = await db.execute(current_defaults_query, {"group_id": group_id})
            current_default_ids = {row[0] for row in current_result.fetchall()}

            new_default_ids = set(sub_agent_ids)
            added_ids = list(new_default_ids - current_default_ids)
            removed_ids = list(current_default_ids - new_default_ids)

            # Validate all sub-agents using SubAgentService (cross-domain delegation)
            if sub_agent_ids:
                await self.sub_agent_service.validate_agents_for_group(
                    db=db,
                    agent_ids=sub_agent_ids,
                    group_id=group_id,
                )

            # Delete only removed defaults
            if removed_ids:
                delete_query = text("""
                    DELETE FROM user_group_default_agents
                    WHERE user_group_id = :group_id AND sub_agent_id = ANY(:removed_ids)
                """)
                await db.execute(delete_query, {"group_id": group_id, "removed_ids": removed_ids})

            # Insert only added defaults
            if added_ids:
                values = []
                params = {"group_id": group_id, "actor_sub": actor_sub}
                for i, agent_id in enumerate(added_ids):
                    values.append(f"(:group_id, :agent_id_{i}, :actor_sub)")
                    params[f"agent_id_{i}"] = agent_id

                insert_query = text(f"""
                    INSERT INTO user_group_default_agents 
                        (user_group_id, sub_agent_id, created_by_user_id)
                    VALUES {", ".join(values)}
                """)
                await db.execute(insert_query, params)

            logger.info(
                f"Set {len(sub_agent_ids)} default agents for group {group_id} (added: {len(added_ids)}, removed: {len(removed_ids)})"
            )

            # Update activations for all existing members
            try:
                # Activate newly added defaults using helper
                if added_ids:
                    for agent_id in added_ids:
                        await self._activate_default_agent(db, group_id, agent_id, actor_sub)
                    logger.info(f"Activated {len(added_ids)} new default agents for group {group_id}")

                # Deactivate removed defaults using helper
                if removed_ids:
                    for agent_id in removed_ids:
                        await self._deactivate_default_agent(db, group_id, agent_id, actor_sub)
                    logger.info(f"Deactivated {len(removed_ids)} removed default agents for group {group_id}")

            except Exception as e:
                logger.error(f"Failed to update activations for existing members: {e}")
                # Continue - don't fail the entire operation

        except Exception as e:
            logger.error(f"Failed to set default agents for group {group_id}: {e}")
            raise

    async def add_group_default_agent(
        self,
        db: AsyncSession,
        group_id: int,
        sub_agent_id: int,
        actor_sub: str,
    ) -> None:
        """Add a single default agent to a group.

        Validates the sub-agent and activates it for all existing members.

        Args:
            db: Database session
            group_id: Group ID
            sub_agent_id: Sub-agent ID to add as default
            actor_sub: User performing the action
        """
        try:
            # Validate the sub-agent
            await self.sub_agent_service.validate_agents_for_group(
                db=db,
                agent_ids=[sub_agent_id],
                group_id=group_id,
            )

            # Check if already a default
            check_query = text("""
                SELECT 1 FROM user_group_default_agents
                WHERE user_group_id = :group_id AND sub_agent_id = :sub_agent_id
            """)
            result = await db.execute(check_query, {"group_id": group_id, "sub_agent_id": sub_agent_id})
            if result.first():
                logger.info(f"Agent {sub_agent_id} is already a default for group {group_id}")
                return

            # Insert the default agent
            insert_query = text("""
                INSERT INTO user_group_default_agents 
                    (user_group_id, sub_agent_id, created_by_user_id)
                VALUES (:group_id, :sub_agent_id, :actor_sub)
            """)
            await db.execute(insert_query, {"group_id": group_id, "sub_agent_id": sub_agent_id, "actor_sub": actor_sub})

            logger.info(f"Added default agent {sub_agent_id} for group {group_id}")

            # Activate for all existing members
            await self._activate_default_agent(db, group_id, sub_agent_id, actor_sub)

        except Exception as e:
            logger.error(f"Failed to add default agent for group {group_id}: {e}")
            raise

    async def remove_group_default_agent(
        self,
        db: AsyncSession,
        group_id: int,
        sub_agent_id: int,
        actor_sub: str,
    ) -> None:
        """Remove a single default agent from a group.

        Deactivates the agent for all group members.

        Args:
            db: Database session
            group_id: Group ID
            sub_agent_id: Sub-agent ID to remove from defaults
            actor_sub: User performing the action
        """
        try:
            # Check if it's currently a default
            check_query = text("""
                SELECT 1 FROM user_group_default_agents
                WHERE user_group_id = :group_id AND sub_agent_id = :sub_agent_id
            """)
            result = await db.execute(check_query, {"group_id": group_id, "sub_agent_id": sub_agent_id})
            if not result.first():
                logger.info(f"Agent {sub_agent_id} is not a default for group {group_id}")
                return

            # Delete the default agent
            delete_query = text("""
                DELETE FROM user_group_default_agents
                WHERE user_group_id = :group_id AND sub_agent_id = :sub_agent_id
            """)
            await db.execute(delete_query, {"group_id": group_id, "sub_agent_id": sub_agent_id})

            logger.info(f"Removed default agent {sub_agent_id} for group {group_id}")

            # Deactivate for all group members
            await self._deactivate_default_agent(db, group_id, sub_agent_id, actor_sub)

        except Exception as e:
            logger.error(f"Failed to remove default agent for group {group_id}: {e}")
            raise
