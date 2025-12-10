"""User group service for managing groups and memberships."""

import logging
from datetime import datetime, timezone
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

logger = logging.getLogger(__name__)


class UserGroupService:
    """Service for managing user groups and memberships."""

    async def get_group(self, db: AsyncSession, group_id: int) -> UserGroup | None:
        """Get a group by ID.

        Args:
            db: Database session
            group_id: Group ID

        Returns:
            Group or None if not found
        """
        query = text("""
            SELECT id, name, description, deleted_at, created_at, updated_at
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
            SELECT id, name, description, deleted_at, created_at, updated_at
            FROM user_groups
            {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)

        try:
            count_result = await db.execute(count_query, params)
            total = count_result.scalar() or 0

            result = await db.execute(data_query, params)
            rows = result.mappings().all()

            groups = []
            for row in rows:
                group = UserGroup(
                    id=row["id"],
                    name=row["name"],
                    description=row["description"],
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
            SELECT ug.id, ug.name, ug.description,
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
        name: str,
        description: str | None = None,
    ) -> UserGroup:
        """Create a new group.

        Args:
            db: Database session
            name: Group name
            description: Group description

        Returns:
            Created group
        """
        query = text("""
            INSERT INTO user_groups (name, description)
            VALUES (:name, :description)
            RETURNING id, name, description, deleted_at, created_at, updated_at
        """)

        try:
            result = await db.execute(
                query,
                {
                    "name": name,
                    "description": description,
                },
            )
            row = result.mappings().first()

            if row is None:
                raise RuntimeError("Failed to create group")

            logger.info(f"Created group: {name}")
            return UserGroup(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to create group: {e}")
            raise

    async def update_group(
        self,
        db: AsyncSession,
        group_id: int,
        name: str | None = None,
        description: str | None = None,
    ) -> UserGroup | None:
        """Update a group.

        Args:
            db: Database session
            group_id: Group ID
            name: New name (optional)
            description: New description (optional)

        Returns:
            Updated group or None if not found
        """
        # Build dynamic update
        updates = ["updated_at = :now"]
        params: dict[str, Any] = {
            "group_id": group_id,
            "now": datetime.now(tz=timezone.utc),
        }

        if name is not None:
            updates.append("name = :name")
            params["name"] = name

        if description is not None:
            updates.append("description = :description")
            params["description"] = description

        query = text(f"""
            UPDATE user_groups
            SET {", ".join(updates)}
            WHERE id = :group_id AND deleted_at IS NULL
            RETURNING id, name, description, deleted_at, created_at, updated_at
        """)

        try:
            result = await db.execute(query, params)
            row = result.mappings().first()

            if row is None:
                return None

            logger.info(f"Updated group: {group_id}")
            return UserGroup(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to update group: {e}")
            raise

    async def delete_group(self, db: AsyncSession, group_id: int, force: bool = False) -> bool:
        """Delete a group (soft delete).

        Args:
            db: Database session
            group_id: Group ID
            force: If True, delete even if sub-agents are assigned

        Returns:
            True if deleted, False if not found or blocked
        """
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

        # Soft delete the group
        query = text("""
            UPDATE user_groups
            SET deleted_at = :now, updated_at = :now
            WHERE id = :group_id AND deleted_at IS NULL
            RETURNING id
        """)

        try:
            now = datetime.now(tz=timezone.utc)
            result = await db.execute(query, {"group_id": group_id, "now": now})
            row = result.mappings().first()

            if row is None:
                return False

            logger.info(f"Deleted group: {group_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete group: {e}")
            raise

    async def bulk_delete_groups(
        self, db: AsyncSession, group_ids: list[int], force: bool = False
    ) -> list[BulkDeleteResult]:
        """Bulk delete groups.

        Args:
            db: Database session
            group_ids: List of group IDs
            force: If True, delete even if sub-agents are assigned

        Returns:
            List of deletion results
        """
        results = []

        for group_id in group_ids:
            try:
                success = await self.delete_group(db, group_id, force)
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
        group_id: int,
        user_ids: list[str],
        role: Literal["read", "write", "manager"] = "read",
    ) -> list[MemberInfo]:
        """Add members to a group.

        Args:
            db: Database session
            group_id: Group ID
            user_ids: List of user IDs to add
            role: Role for the new members

        Returns:
            Updated member list
        """
        query = text("""
            INSERT INTO user_group_members (user_id, user_group_id, group_role)
            VALUES (:user_id, :group_id, :role)
            ON CONFLICT (user_id, user_group_id) DO UPDATE SET group_role = :role
        """)

        try:
            for user_id in user_ids:
                await db.execute(
                    query,
                    {"user_id": user_id, "group_id": group_id, "role": role},
                )

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
        group_id: int,
        user_id: str,
        role: Literal["read", "write", "manager"],
    ) -> MemberInfo | None:
        """Update a member's role.

        Args:
            db: Database session
            group_id: Group ID
            user_id: User ID
            role: New role

        Returns:
            Updated member info or None if not found
        """
        query = text("""
            UPDATE user_group_members
            SET group_role = :role
            WHERE user_group_id = :group_id AND user_id = :user_id
            RETURNING id
        """)

        try:
            result = await db.execute(
                query,
                {"group_id": group_id, "user_id": user_id, "role": role},
            )
            row = result.mappings().first()

            if row is None:
                return None

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

    async def remove_member(self, db: AsyncSession, group_id: int, user_id: str) -> bool:
        """Remove a member from a group.

        Cannot remove the last admin.

        Args:
            db: Database session
            group_id: Group ID
            user_id: User ID to remove

        Returns:
            True if removed, False if not found
        """
        # Check if this is the last manager
        admin_count_query = text("""
            SELECT COUNT(*) as count
            FROM user_group_members
            WHERE user_group_id = :group_id AND group_role = 'manager'
        """)

        check_role_query = text("""
            SELECT group_role FROM user_group_members
            WHERE user_group_id = :group_id AND user_id = :user_id
        """)

        try:
            # Check if user is manager
            role_result = await db.execute(check_role_query, {"group_id": group_id, "user_id": user_id})
            role_row = role_result.mappings().first()

            if role_row is None:
                return False

            if role_row["group_role"] == "manager":
                # Check manager count
                admin_result = await db.execute(admin_count_query, {"group_id": group_id})
                admin_count = admin_result.scalar() or 0

                if admin_count <= 1:
                    raise ValueError("Cannot remove the last manager from a group")

            # Remove member
            delete_query = text("""
                DELETE FROM user_group_members
                WHERE user_group_id = :group_id AND user_id = :user_id
                RETURNING id
            """)
            result = await db.execute(delete_query, {"group_id": group_id, "user_id": user_id})
            row = result.mappings().first()

            if row is None:
                return False

            logger.info(f"Removed member {user_id} from group {group_id}")
            return True
        except ValueError:
            raise
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


# Module-level singleton
user_group_service = UserGroupService()
