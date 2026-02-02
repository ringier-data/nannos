"""User service for managing users in PostgreSQL."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.user import (
    BulkOperationResult,
    BulkUserOperation,
    User,
    UserGroupMembership,
    UserStatus,
    UserWithGroups,
)
from ..repositories.user_repository import UserRepository
from ..services.audit_service import AuditService

logger = logging.getLogger(__name__)


class UserService:
    """Manages users in PostgreSQL."""

    def __init__(self, user_repository: UserRepository | None = None, audit_service: AuditService | None = None):
        """Initialize user service.

        Args:
            user_repository: Optional user repository instance.
                If None, must be set via set_repository() before use.
            audit_service: Optional audit service instance.
                If None, must be set via set_audit_service() before use.
        """
        self._repo = user_repository
        self._audit_service = audit_service

    def set_repository(self, user_repository: UserRepository):
        """Set the user repository (dependency injection)."""
        self._repo = user_repository

    def set_audit_service(self, audit_service: AuditService):
        """Set the audit service (dependency injection)."""
        self._audit_service = audit_service

    @property
    def repo(self) -> UserRepository:
        """Get the user repository, raising error if not set."""
        if self._repo is None:
            raise RuntimeError("UserRepository not injected. Call set_repository() during initialization.")
        return self._repo

    @property
    def audit_service(self) -> AuditService:
        """Get the audit service, raising error if not set."""
        if self._audit_service is None:
            raise RuntimeError("AuditService not injected. Call set_audit_service() during initialization.")
        return self._audit_service

    async def get_user(self, db: AsyncSession, user_id: str) -> User | None:
        """Retrieve a user by ID.

        Args:
            db: The database session
            user_id: The user's ID (sub from OIDC)

        Returns:
            The user or None if not found
        """
        try:
            query = text("""
                SELECT id, sub, email, first_name, last_name, company_name,
                       is_administrator, role, status, deleted_at, created_at, updated_at
                FROM users
                WHERE id = :user_id
            """)
            result = await db.execute(query, {"user_id": user_id})
            row = result.mappings().first()

            if row is None:
                logger.debug(f"User not found: {user_id}")
                return None

            return User(
                id=row["id"],
                sub=row["sub"],
                email=row["email"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                company_name=row["company_name"],
                is_administrator=row["is_administrator"],
                role=row["role"],
                status=UserStatus(row["status"]),
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get user: {e}")
            return None

    async def get_user_by_sub(self, db: AsyncSession, sub: str) -> User | None:
        """Retrieve a user by OIDC subject (sub).

        Args:
            db: The database session
            sub: The user's OIDC subject

        Returns:
            The user or None if not found
        """
        try:
            query = text("""
                SELECT id, sub, email, first_name, last_name, company_name,
                       is_administrator, role, status, deleted_at, created_at, updated_at
                FROM users
                WHERE sub = :sub
            """)
            result = await db.execute(query, {"sub": sub})
            row = result.mappings().first()

            if row is None:
                logger.debug(f"User not found by sub: {sub}")
                return None

            return User(
                id=row["id"],
                sub=row["sub"],
                email=row["email"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                company_name=row["company_name"],
                is_administrator=row["is_administrator"],
                role=row["role"],
                status=UserStatus(row["status"]),
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get user by sub: {e}")
            return None

    async def get_user_with_groups(self, db: AsyncSession, user_id: str) -> UserWithGroups | None:
        """Retrieve a user by ID with group memberships.

        Args:
            db: The database session
            user_id: The user's ID

        Returns:
            The user with groups or None if not found
        """
        user = await self.get_user(db, user_id)
        if user is None:
            return None

        # Fetch group memberships
        groups_query = text("""
            SELECT ug.id as group_id, ug.name as group_name, ugm.group_role
            FROM user_group_members ugm
            JOIN user_groups ug ON ug.id = ugm.user_group_id
            WHERE ugm.user_id = :user_id
            AND ug.deleted_at IS NULL
        """)
        result = await db.execute(groups_query, {"user_id": user_id})
        group_rows = result.mappings().all()

        groups = [
            UserGroupMembership(
                group_id=row["group_id"],
                group_name=row["group_name"],
                group_role=row["group_role"],
            )
            for row in group_rows
        ]

        return UserWithGroups(
            **user.model_dump(),
            groups=groups,
        )

    async def list_users(
        self,
        db: AsyncSession,
        page: int = 1,
        limit: int = 20,
        search: str | None = None,
        group_id: int | None = None,
        include_deleted: bool = False,
    ) -> tuple[list[UserWithGroups], int]:
        """List users with pagination and filtering.

        Args:
            db: Database session
            page: Page number (1-indexed)
            limit: Items per page
            search: Search term for name/email
            group_id: Filter by group membership
            include_deleted: Whether to include deleted users

        Returns:
            Tuple of (users with groups, total count)
        """
        # Build WHERE clauses
        conditions = []
        params: dict[str, Any] = {
            "limit": limit,
            "offset": (page - 1) * limit,
        }

        if not include_deleted:
            conditions.append("u.status != 'deleted'")
            conditions.append("u.deleted_at IS NULL")

        if search:
            conditions.append("""
                (u.first_name ILIKE :search
                OR u.last_name ILIKE :search
                OR u.email ILIKE :search)
            """)
            params["search"] = f"%{search}%"

        if group_id:
            conditions.append("""
                EXISTS (
                    SELECT 1 FROM user_group_members ugm
                    WHERE ugm.user_id = u.id AND ugm.user_group_id = :group_id
                )
            """)
            params["group_id"] = group_id

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        # Count query
        count_query = text(f"""
            SELECT COUNT(*) as total
            FROM users u
            {where_clause}
        """)

        # Data query - get users
        data_query = text(f"""
            SELECT u.id, u.sub, u.email, u.first_name, u.last_name, u.company_name,
                   u.is_administrator, u.role, u.status, u.deleted_at,
                   u.created_at, u.updated_at
            FROM users u
            {where_clause}
            ORDER BY u.created_at DESC
            LIMIT :limit OFFSET :offset
        """)

        try:
            # Get total count
            count_result = await db.execute(count_query, params)
            total = count_result.scalar() or 0

            # Get users
            result = await db.execute(data_query, params)
            user_rows = result.mappings().all()

            users_with_groups = []
            for row in user_rows:
                user = User(
                    id=row["id"],
                    sub=row["sub"],
                    email=row["email"],
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    company_name=row["company_name"],
                    is_administrator=row["is_administrator"],
                    role=row["role"],
                    status=UserStatus(row["status"]),
                    deleted_at=row["deleted_at"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )

                # Fetch groups for each user
                user_with_groups = await self.get_user_with_groups(db, user.id)
                if user_with_groups:
                    users_with_groups.append(user_with_groups)

            return users_with_groups, total
        except Exception as e:
            logger.error(f"Failed to list users: {e}")
            raise

    async def update_user_status(self, db: AsyncSession, user_id: str, actor: User, status: UserStatus) -> User | None:
        """Update a user's status and optionally soft delete.

        Args:
            db: Database session
            user_id: The user's ID to update
            actor: User performing the action
            status: New status

        Returns:
            Updated user or None if not found
        """
        try:
            # Update status via repository (with audit)
            await self.repo.update_status(db, user_id, actor, status.value)

            # Handle soft delete if needed
            if status == UserStatus.DELETED:
                now = datetime.now(tz=timezone.utc)
                await db.execute(
                    text("UPDATE users SET deleted_at = :deleted_at WHERE id = :user_id"),
                    {"user_id": user_id, "deleted_at": now},
                )

            logger.info(f"Updated user {user_id} status to {status.value}")
            return await self.get_user(db, user_id)
        except ValueError:
            logger.warning(f"User not found for status update: {user_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to update user status: {e}")
            raise

    async def update_user_groups(
        self,
        db: AsyncSession,
        user_id: str,
        actor: User,
        group_ids: list[int],
        operation: Literal["set", "add", "remove"],
    ) -> UserWithGroups | None:
        """Update a user's group memberships.

        Args:
            db: Database session
            user_id: The user's ID to update
            actor: User performing the action
            group_ids: List of group IDs
            operation: 'set' replaces all, 'add' adds to existing, 'remove' removes

        Returns:
            Updated user with groups or None if user not found
        """
        # Verify user exists
        user = await self.get_user(db, user_id)
        if user is None:
            return None

        try:
            if operation == "set":
                # Use repository for full replacement (with audit)
                await self.repo.update_groups(db, user_id, actor, group_ids)
            elif operation == "add":
                for group_id in group_ids:
                    await db.execute(
                        text("""
                            INSERT INTO user_group_members (user_id, user_group_id, group_role)
                            VALUES (:user_id, :group_id, 'read')
                            ON CONFLICT (user_id, user_group_id) DO NOTHING
                        """),
                        {"user_id": user_id, "group_id": group_id},
                    )
            elif operation == "remove":
                for group_id in group_ids:
                    await db.execute(
                        text("""
                            DELETE FROM user_group_members
                            WHERE user_id = :user_id AND user_group_id = :group_id
                        """),
                        {"user_id": user_id, "group_id": group_id},
                    )

            logger.info(f"Updated groups for user {user_id}: {operation} {group_ids}")
            return await self.get_user_with_groups(db, user_id)
        except Exception as e:
            logger.error(f"Failed to update user groups: {e}")
            raise

    async def bulk_update_users(
        self, db: AsyncSession, actor: User, operations: list[BulkUserOperation]
    ) -> list[BulkOperationResult]:
        """Perform bulk user status updates.

        Args:
            db: Database session
            actor: User performing the action
            operations: List of operations to perform

        Returns:
            List of operation results
        """
        results = []

        for op in operations:
            try:
                status_map = {
                    "suspend": UserStatus.SUSPENDED,
                    "activate": UserStatus.ACTIVE,
                    "delete": UserStatus.DELETED,
                }
                new_status = status_map.get(op.action)

                if new_status is None:
                    results.append(
                        BulkOperationResult(
                            user_id=op.user_id,
                            success=False,
                            error=f"Unknown action: {op.action}",
                        )
                    )
                    continue

                # Use repository for status update (with automatic audit per user)
                success = await self.repo.bulk_update_status(db, op.user_id, actor, new_status.value)

                if not success:
                    results.append(
                        BulkOperationResult(
                            user_id=op.user_id,
                            success=False,
                            error="User not found",
                        )
                    )
                else:
                    results.append(
                        BulkOperationResult(
                            user_id=op.user_id,
                            success=True,
                        )
                    )
            except Exception as e:
                results.append(
                    BulkOperationResult(
                        user_id=op.user_id,
                        success=False,
                        error=str(e),
                    )
                )

        return results

    async def upsert_user(
        self,
        db: AsyncSession,
        sub: str,
        email: str,
        first_name: str,
        last_name: str,
        company_name: str | None = None,
    ) -> User:
        """Create or update a user using PostgreSQL upsert.

        This uses INSERT ... ON CONFLICT to atomically create or update.
        OIDC-sourced fields are always updated, while user-editable fields
        (is_administrator) are only set on initial creation.

        Args:
            db: The database session
            sub: The user's sub from OIDC
            email: The user's email
            first_name: The user's first name
            last_name: The user's last name
            company_name: The user's company name (optional)

        Returns:
            The created or updated user
        """
        now = datetime.now(tz=timezone.utc)

        query = text("""
            INSERT INTO users (id, sub, email, first_name, last_name, company_name,
                               is_administrator, role, status, created_at, updated_at)
            VALUES (:id, :sub, :email, :first_name, :last_name, :company_name,
                    FALSE, 'member', 'active', :now, :now)
            ON CONFLICT (id) DO UPDATE SET
                sub = EXCLUDED.sub,
                email = EXCLUDED.email,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                company_name = EXCLUDED.company_name,
                updated_at = EXCLUDED.updated_at
            RETURNING id, sub, email, first_name, last_name, company_name,
                      is_administrator, role, status, deleted_at, created_at, updated_at
        """)

        try:
            email = email.lower().strip()
            # Check if user exists before upsert
            check_query = text("SELECT id, sub, email FROM users WHERE email = :email OR sub = :sub")
            results = await db.execute(check_query, {"email": email, "sub": sub})
            rows = results.mappings().all()
            if len(rows) > 1:
                raise ValueError(f"Multiple users found with email {email} or sub {sub}")

            row = rows[0] if rows else None
            user_id = row["id"] if row else None
            old_sub = row["sub"] if row else None
            old_email = row["email"] if row else None

            result = await db.execute(
                query,
                {
                    "id": user_id if user_id else str(uuid.uuid4()),
                    "sub": sub,
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                    "company_name": company_name,
                    "now": now,
                },
            )
            row = result.mappings().first()

            if row is None:
                raise RuntimeError(f"upsert returned None for user {sub}")

            # Create User object from the upserted data (for audit actor)
            user = User(
                id=row["id"],
                sub=row["sub"],
                email=row["email"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                company_name=row["company_name"],
                is_administrator=row["is_administrator"],
                role=row["role"],
                status=UserStatus(row["status"]),
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

            # Audit creation or identifier/email changes (user is actor for self-service operations)
            if user_id is None:
                # New user creation
                await self.audit_service.log_action(
                    db=db,
                    actor=user,  # User creates themselves via OIDC
                    entity_type=AuditEntityType.USER,
                    entity_id=row["id"],
                    action=AuditAction.CREATE,
                    changes={
                        "after": {
                            "email": email,
                            "first_name": first_name,
                            "last_name": last_name,
                            "company_name": company_name,
                        }
                    },
                )
                logger.info(f"Created new user (audited): {sub}")
            else:
                if sub != old_sub:
                    # Audit sub change if it differs from previous
                    await self.audit_service.log_action(
                        db=db,
                        actor=user,
                        entity_type=AuditEntityType.USER,
                        entity_id=user_id,
                        action=AuditAction.UPDATE,
                        changes={
                            "before": {
                                "old_sub": old_sub,
                            },
                            "after": {
                                "new_sub": sub,
                            },
                        },
                    )
                if email != old_email:
                    # Audit email change if it differs from previous
                    await self.audit_service.log_action(
                        db=db,
                        actor=user,
                        entity_type=AuditEntityType.USER,
                        entity_id=user_id,
                        action=AuditAction.UPDATE,
                        changes={
                            "before": {
                                "old_email": old_email,
                            },
                            "after": {
                                "new_email": email,
                            },
                        },
                    )

            return user
        except IntegrityError as e:
            # Handle unique email constraint violation
            if "idx_users_email_unique" in str(e):
                logger.error(f"Email already exists for a different user: {email}")
                raise ValueError(f"Email {email} is already registered to a different account")
            logger.error(f"Database integrity error during user upsert: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to upsert user: {e}")
            raise

    async def update_user_admin_fields(
        self,
        db: AsyncSession,
        user_id: str,
        actor: User,
        is_administrator: bool | None = None,
    ) -> User | None:
        """Update admin-controlled user fields.

        Args:
            db: Database session
            user_id: The user's ID
            actor_sub: ID of user performing the action
            is_administrator: New administrator status

        Returns:
            Updated user or None if not found
        """
        if is_administrator is None:
            # No fields to update, just return current user
            return await self.get_user(db, user_id)

        try:
            # Update via repository (with audit)
            await self.repo.update_admin_fields(db, user_id, actor, is_administrator)
            logger.info(f"Updated admin fields for user {user_id}")
            return await self.get_user(db, user_id)
        except ValueError:
            logger.warning(f"User not found for admin field update: {user_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to update user admin fields: {e}")
            raise

    async def update_user_role(
        self,
        db: AsyncSession,
        user_id: str,
        actor: User,
        role: str,
    ) -> User | None:
        """Update a user's role.

        Args:
            db: Database session
            user_id: The user's ID
            actor: User performing the action
            role: New role (viewer, developer, approver, admin)

        Returns:
            Updated user or None if not found
        """
        try:
            # Update via repository (with audit)
            await self.repo.update_role(db, user_id, actor, role)
            logger.info(f"Updated role for user {user_id} to {role}")
            return await self.get_user(db, user_id)
        except ValueError:
            logger.warning(f"User not found for role update: {user_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to update user role: {e}")
            raise
