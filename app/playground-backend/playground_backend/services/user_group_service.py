"""User group service for managing groups and memberships."""

import asyncio
import logging
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..authorization import (
    SYSTEM_ROLE_CAPABILITIES,
    check_action_allowed,
)
from ..models.user_group import (
    BulkDeleteResult,
    MemberInfo,
    UserGroup,
    UserGroupWithMembers,
)
from ..repositories.user_group_repository import UserGroupRepository
from ..services.keycloak_admin_service import KeycloakAdminService

logger = logging.getLogger(__name__)


class UserGroupService:
    """Service for managing user groups and memberships."""

    def __init__(
        self,
        user_group_repository: UserGroupRepository | None = None,
        keycloak_admin_service: KeycloakAdminService | None = None,
    ):
        """Initialize user group service.

        Args:
            user_group_repository: Optional user group repository instance.
                If None, must be set via set_repository() before use.
            keycloak_admin_service: Optional Keycloak admin service for group sync.
                If None, Keycloak sync will be skipped.
        """
        self._repo = user_group_repository
        self._keycloak_service = keycloak_admin_service

    def set_repository(self, user_group_repository):
        """Set the user group repository (dependency injection)."""
        self._repo = user_group_repository

    def set_keycloak_service(self, keycloak_admin_service: KeycloakAdminService | None):
        """Set the Keycloak admin service (dependency injection)."""
        self._keycloak_service = keycloak_admin_service

    @property
    def repo(self):
        """Get the user group repository, raising error if not set."""
        if self._repo is None:
            raise RuntimeError("UserGroupRepository not injected. Call set_repository() during initialization.")
        return self._repo

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
        """Add members to a group.

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

            # Return updated member list
            members, _ = await self.list_members(db, group_id, page=1, limit=1000)
            return members
        except Exception as e:
            logger.error(f"Failed to add members: {e}")
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

        try:
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
            # Return updated member list
            members, _ = await self.list_members(db, group_id, page=1, limit=1000)
            return members
        except Exception as e:
            logger.error(f"Failed to remove members: {e}")
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
