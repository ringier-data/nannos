"""User service for managing users in PostgreSQL."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

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
    has_idp_identity,
)
from ..repositories.user_repository import UserRepository
from ..services.audit_service import AuditService
from ..services.keycloak_admin_service import KeycloakAdminService
from ..utils.sql_search import like_clause, like_contains

logger = logging.getLogger(__name__)


class UserService:
    """Manages users in PostgreSQL."""

    def __init__(
        self,
        user_repository: UserRepository | None = None,
        audit_service: AuditService | None = None,
        keycloak_admin_service: KeycloakAdminService | None = None,
    ):
        """Initialize user service.

        Args:
            user_repository: Optional user repository instance.
                If None, must be set via set_repository() before use.
            audit_service: Optional audit service instance.
                If None, must be set via set_audit_service() before use.
            keycloak_admin_service: Optional Keycloak admin service, used to push the group
                memberships a SCIM-provisioned user accumulated before their first login.
                Left None when Keycloak group sync is disabled.
        """
        self._repo = user_repository
        self._audit_service = audit_service
        self._keycloak_service = keycloak_admin_service

    def set_repository(self, user_repository: UserRepository):
        """Set the user repository (dependency injection)."""
        self._repo = user_repository

    def set_audit_service(self, audit_service: AuditService):
        """Set the audit service (dependency injection)."""
        self._audit_service = audit_service

    def set_keycloak_service(self, keycloak_admin_service: KeycloakAdminService):
        """Set the Keycloak admin service (dependency injection)."""
        self._keycloak_service = keycloak_admin_service

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
                       is_administrator, is_service_account, role, status, phone_number_idp,
                       scim_attributes, deleted_at, created_at, updated_at
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
                is_service_account=row["is_service_account"],
                role=row["role"],
                status=UserStatus(row["status"]),
                phone_number_idp=row["phone_number_idp"],
                scim_attributes=row["scim_attributes"],
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
                       is_administrator, is_service_account, role, status, phone_number_idp,
                       scim_attributes, deleted_at, created_at, updated_at
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
                is_service_account=row["is_service_account"],
                role=row["role"],
                status=UserStatus(row["status"]),
                phone_number_idp=row["phone_number_idp"],
                scim_attributes=row["scim_attributes"],
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get user by sub: {e}")
            return None

    async def get_user_by_email(self, db: AsyncSession, email: str) -> User | None:
        """Retrieve a user by email (case-insensitive).

        Email carries a unique constraint (idx_users_email_unique), so this is a
        1:1 lookup. Used by the cross-IdP federated-exchange path (ADR-0002
        Amendment 2) to resolve a foreign identity to a *provisioned* nannos user
        — a user existing here is the provisioned-link guardrail.

        Args:
            db: The database session
            email: The user's email address

        Returns:
            The user or None if not found
        """
        try:
            query = text("""
                SELECT id, sub, email, first_name, last_name, company_name,
                       is_administrator, is_service_account, role, status, phone_number_idp,
                       scim_attributes, deleted_at, created_at, updated_at
                FROM users
                WHERE lower(email) = lower(:email) AND deleted_at IS NULL
            """)
            result = await db.execute(query, {"email": email})
            row = result.mappings().first()

            if row is None:
                logger.debug("User not found by email")
                return None

            return User(
                id=row["id"],
                sub=row["sub"],
                email=row["email"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                company_name=row["company_name"],
                is_administrator=row["is_administrator"],
                is_service_account=row["is_service_account"],
                role=row["role"],
                status=UserStatus(row["status"]),
                phone_number_idp=row["phone_number_idp"],
                scim_attributes=row["scim_attributes"],
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get user by email: {e}")
            return None

    async def get_user_by_phone_number(self, db: AsyncSession, phone_number: str) -> User | None:
        """Retrieve a user by phone number.

        Checks phone_number_override (user_settings) first, then phone_number_idp (users table).

        Args:
            db: The database session
            phone_number: E.164 phone number string

        Returns:
            The user or None if not found
        """
        try:
            query = text("""
                SELECT u.id, u.sub, u.email, u.first_name, u.last_name, u.company_name,
                       u.is_administrator, u.is_service_account, u.role, u.status, u.phone_number_idp,
                       u.scim_attributes, u.deleted_at, u.created_at, u.updated_at
                FROM users u
                LEFT JOIN user_settings us ON u.id = us.user_id
                WHERE (us.phone_number_override = :phone_number
                   OR u.phone_number_idp = :phone_number)
                  AND u.status != 'deleted'
                LIMIT 1
            """)
            result = await db.execute(query, {"phone_number": phone_number})
            row = result.mappings().first()

            if row is None:
                logger.debug("User not found by phone number")
                return None

            return User(
                id=row["id"],
                sub=row["sub"],
                email=row["email"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                company_name=row["company_name"],
                is_administrator=row["is_administrator"],
                is_service_account=row["is_service_account"],
                role=row["role"],
                status=UserStatus(row["status"]),
                phone_number_idp=row["phone_number_idp"],
                scim_attributes=row["scim_attributes"],
                deleted_at=row["deleted_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get user by phone number: {e}")
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
        exclude_group_id: int | None = None,
        status: UserStatus | None = None,
        include_deleted: bool = False,
    ) -> tuple[list[UserWithGroups], int]:
        """List users with pagination and filtering.

        Args:
            db: Database session
            page: Page number (1-indexed)
            limit: Items per page
            search: Search term for name/email
            group_id: Filter by group membership
            exclude_group_id: Drop users who are already members of this group
            status: Keep only users in this status
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
            conditions.append(like_clause("u.first_name", "u.last_name", "u.email"))
            params["search"] = like_contains(search)

        if group_id:
            conditions.append("""
                EXISTS (
                    SELECT 1 FROM user_group_members ugm
                    WHERE ugm.user_id = u.id AND ugm.user_group_id = :group_id
                )
            """)
            params["group_id"] = group_id

        # Backs the "add members" picker: the candidates are everyone the group
        # does not already have, decided over the whole table rather than over
        # whichever page of members the caller happens to be looking at.
        if exclude_group_id:
            conditions.append("""
                NOT EXISTS (
                    SELECT 1 FROM user_group_members ugm
                    WHERE ugm.user_id = u.id AND ugm.user_group_id = :exclude_group_id
                )
            """)
            params["exclude_group_id"] = exclude_group_id

        if status:
            conditions.append("u.status = :status")
            params["status"] = status.value if isinstance(status, UserStatus) else status

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
                   u.is_administrator, u.is_service_account, u.role, u.status,
                   u.phone_number_idp, u.scim_attributes, u.deleted_at,
                   u.created_at, u.updated_at
            FROM users u
            {where_clause}
            ORDER BY u.created_at DESC, u.id DESC
            LIMIT :limit OFFSET :offset
        """)

        try:
            # Get total count
            count_result = await db.execute(count_query, params)
            total = count_result.scalar() or 0

            # Get users
            result = await db.execute(data_query, params)
            user_rows = result.mappings().all()

            users: list[User] = []
            for row in user_rows:
                user = User(
                    id=row["id"],
                    sub=row["sub"],
                    email=row["email"],
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    company_name=row["company_name"],
                    is_administrator=row["is_administrator"],
                    is_service_account=row["is_service_account"],
                    role=row["role"],
                    status=UserStatus(row["status"]),
                    phone_number_idp=row["phone_number_idp"],
                    scim_attributes=row["scim_attributes"],
                    deleted_at=row["deleted_at"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                users.append(user)

            # Memberships for the whole page in one round trip. Asking per user
            # made this endpoint 2+2N queries, and on a containerised Postgres a
            # round trip dominates the actual work: a 6-user page spent ~500ms
            # almost entirely waiting. The per-user call also re-read the very
            # row the query above had just returned.
            groups_by_user: dict[str, list[UserGroupMembership]] = {}
            if users:
                memberships = await db.execute(
                    text("""
                        SELECT ugm.user_id, ug.id as group_id, ug.name as group_name, ugm.group_role
                        FROM user_group_members ugm
                        JOIN user_groups ug ON ug.id = ugm.user_group_id
                        WHERE ugm.user_id = ANY(:user_ids)
                        AND ug.deleted_at IS NULL
                    """),
                    {"user_ids": [u.id for u in users]},
                )
                for row in memberships.mappings().all():
                    groups_by_user.setdefault(row["user_id"], []).append(
                        UserGroupMembership(
                            group_id=row["group_id"],
                            group_name=row["group_name"],
                            group_role=row["group_role"],
                        )
                    )

            users_with_groups = [
                UserWithGroups(**user.model_dump(), groups=groups_by_user.get(user.id, []))
                for user in users
            ]

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

    async def _sync_pending_group_memberships(self, db: AsyncSession, user_id: str, sub: str) -> None:
        """Mirror a user's group memberships into Keycloak and clear the pending flag.

        Runs at login for a user `users.keycloak_mirror_pending` marks as diverged: one
        provisioned over SCIM whose membership changes were skipped while they had no IdP
        account (`UserGroupService._mirror_membership_to_keycloak`).

        The database is the authority for membership (`/api/v1/auth/me` reads it from there);
        Keycloak only backs the groups claim other OIDC clients consume. A failure to mirror
        must therefore not fail the login, so it is logged rather than raised — and the flag
        stays set, so the next login tries again. Pushing the *current* membership set rather
        than a recorded backlog is what makes that retry safe: the calls are idempotent, and a
        membership granted and withdrawn while the user was pending needs no replay.
        """
        if self._keycloak_service is None:
            # No Keycloak configured: leave the flag set. If group sync is switched on later,
            # the divergence is still recorded and the next login pushes it.
            return

        # Deliberately unguarded: this runs inside the caller's transaction, so a database error
        # here has already poisoned it and would resurface at the next statement anyway. Swallowing
        # it would only misattribute the failure. What must not fail the login is the Keycloak call
        # below, and that is what is caught.
        result = await db.execute(
            text("""
                SELECT ug.keycloak_group_id
                FROM user_groups ug
                JOIN user_group_members ugm ON ugm.user_group_id = ug.id
                WHERE ugm.user_id = :user_id
                  AND ug.deleted_at IS NULL
                  AND ug.keycloak_group_id IS NOT NULL
            """),
            {"user_id": user_id},
        )
        group_ids = [row["keycloak_group_id"] for row in result.mappings().all()]
        if not group_ids:
            # Nothing to mirror — whatever the flag was raised for is gone (the membership was
            # withdrawn, or the group left Keycloak). The user is not diverged any more.
            await self._clear_mirror_pending(db, user_id)
            return

        # Concurrently: these are HTTP round-trips held open across the login transaction, and a
        # user in a dozen groups should not pay for them one at a time.
        outcomes = await asyncio.gather(
            *(self._keycloak_service.add_user_to_group(sub, group_id) for group_id in group_ids),
            return_exceptions=True,
        )
        for group_id, outcome in zip(group_ids, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # One unreachable group must not cost the user the others, nor their login.
                logger.error(f"Failed to reconcile user {user_id} into Keycloak group {group_id}: {outcome}")
            else:
                logger.info(f"Reconciled user {user_id} (sub={sub}) into Keycloak group {group_id}")

        if any(isinstance(outcome, BaseException) for outcome in outcomes):
            # Leave the flag set: this user is still diverged, and the next login retries.
            logger.warning(
                f"Keycloak mirror for user {user_id} is still pending after {len(group_ids)} group(s); "
                f"it will be retried at their next login"
            )
            return

        await self._clear_mirror_pending(db, user_id)

    async def _clear_mirror_pending(self, db: AsyncSession, user_id: str) -> None:
        """Record that Keycloak is no longer behind for this user."""
        await db.execute(
            text("UPDATE users SET keycloak_mirror_pending = FALSE WHERE id = :user_id"),
            {"user_id": user_id},
        )

    @staticmethod
    def _pick_existing_row(rows: Any, *, sub: str, email: str) -> Any:
        """Which existing row, if any, this login belongs to.

        More than one row can match. Migration 051 made the email index partial
        (`WHERE deleted_at IS NULL`) *deliberately*, so that soft-deleting a user frees their
        address for re-provisioning — which means a soft-deleted row and a live row can share
        an email, and the query's two arms can land on different people.

        The subject settles it whenever it matches: `users.sub` is unique and is who the token
        says this is. Only when no row matches by subject does the email arm decide anything,
        and there the live row is the one being adopted — this is the SCIM-placeholder path,
        and a soft-deleted namesake is not the account being logged into.

        Preferring the live row *without* checking the subject first would be the dangerous
        version of this: a soft-deleted user logging in would be resolved onto the live row
        that inherited their address, handing them somebody else's account.
        """
        if not rows:
            return None

        by_sub = [row for row in rows if row["sub"] == sub]
        if by_sub:
            return by_sub[0]

        live = [row for row in rows if row["deleted_at"] is None]
        if len(live) > 1:
            # The partial unique index should make this unreachable; it is a real ambiguity.
            raise ValueError(f"Multiple live users found with email {email} or sub {sub}")
        if live:
            return live[0]

        if len(rows) > 1:
            raise ValueError(f"Multiple users found with email {email} or sub {sub}")
        return rows[0]

    async def _upsert_user_internal(
        self,
        db: AsyncSession,
        sub: str,
        email: str,
        first_name: str,
        last_name: str,
        phone_number_idp: str | None,
        company_name: str | None,
        is_service_account: bool = False,
        retries_left: int = 1,
    ) -> User:
        """Internal method to upsert user with retry on IntegrityError.
        There is the chance that multiple concurrent upserts could violate unique constraints
        in this case raising IntegrityError is not appropriate, because in theory one of them
        should succeed. Thus we retry once.
        """
        email = email.lower().strip()
        # Check if user exists before upsert
        # LOWER(email), not `email = :email`: SCIM stores `userName`/`emails[].value` verbatim while
        # this path lowercases, so a mixed-case SCIM address would never match its own row — the
        # login would try to insert a second one and trip `idx_users_email_unique` (which is itself
        # on LOWER(email)), and the placeholder subject would never be reconciled.
        check_query = text(
            "SELECT id, sub, email, keycloak_mirror_pending, deleted_at FROM users "
            "WHERE LOWER(email) = :email OR sub = :sub"
        )
        results = await db.execute(check_query, {"email": email, "sub": sub})
        rows = results.mappings().all()
        row = self._pick_existing_row(rows, sub=sub, email=email)
        user_id = row["id"] if row else None
        old_sub = row["sub"] if row else None
        old_email = row["email"] if row else None
        mirror_pending = bool(row["keycloak_mirror_pending"]) if row else False
        try:
            now = datetime.now(tz=timezone.utc)

            query = text("""
                INSERT INTO users (id, sub, email, first_name, last_name, company_name,
                                is_administrator, is_service_account, role, status,
                                phone_number_idp, created_at, updated_at)
                VALUES (:id, :sub, :email, :first_name, :last_name, :company_name,
                        FALSE, :is_service_account, 'member', 'active', :phone_number_idp, :now, :now)
                ON CONFLICT (id) DO UPDATE SET
                    sub = EXCLUDED.sub,
                    email = EXCLUDED.email,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    company_name = EXCLUDED.company_name,
                    phone_number_idp = COALESCE(EXCLUDED.phone_number_idp, users.phone_number_idp),
                    -- Monotonic: the token can raise this flag on an existing row but
                    -- never clear it. Migration 088 backfills machine rows by Keycloak's
                    -- `service-account-` email convention, and a client-credentials token
                    -- need not carry an email at all — so a service account onboarded
                    -- before that migration can sit unflagged forever if the flag is only
                    -- ever set on insert. Raising it here lets the account correct itself
                    -- on its next call. The trade-off is deliberate: an operator who
                    -- clears the flag on a row whose token still says
                    -- `service-account-<client>` will see it come back.
                    is_service_account = users.is_service_account OR EXCLUDED.is_service_account,
                    updated_at = EXCLUDED.updated_at
                RETURNING id, sub, email, first_name, last_name, company_name,
                        is_administrator, is_service_account, role, status, phone_number_idp,
                        deleted_at, created_at, updated_at
            """)

            result = await db.execute(
                query,
                {
                    "id": user_id if user_id else str(uuid.uuid4()),
                    "sub": sub,
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                    "company_name": company_name,
                    "phone_number_idp": phone_number_idp,
                    "is_service_account": is_service_account,
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
                is_service_account=row["is_service_account"],
                role=row["role"],
                status=UserStatus(row["status"]),
                phone_number_idp=row["phone_number_idp"],
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

                # First user in the system becomes admin automatically.
                # Controlled by FIRST_USER_IS_ADMIN env var (default: false).
                # Disable in production if using a separate admin seed process.
                if os.getenv("FIRST_USER_IS_ADMIN", "false").lower() in ("true", "1", "yes"):
                    count_result = await db.execute(text("SELECT COUNT(*) FROM users"))
                    total_users = count_result.scalar()
                    if total_users == 1:
                        await db.execute(
                            text("UPDATE users SET is_administrator = TRUE WHERE id = :id"),
                            {"id": user.id},
                        )
                        user.is_administrator = True
                        logger.info(f"First user {sub} auto-promoted to administrator (FIRST_USER_IS_ADMIN=true)")
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

            # Keycloak is behind for this user: memberships granted while they had no IdP
            # account were skipped. Driven by the stored flag rather than by the
            # placeholder-to-real subject transition, so a push that fails (Keycloak down,
            # admin credentials absent at boot) is retried at their next login instead of
            # being lost with the one moment that transition happened.
            if mirror_pending and has_idp_identity(sub):
                await self._sync_pending_group_memberships(db, user.id, sub)

            return user
        except IntegrityError as e:
            if retries_left > 0 and "users_sub_key" in str(e):
                logger.warning(f"IntegrityError during upsert for user {sub}: {e}. Retrying once.")
                await db.rollback()
                await asyncio.sleep(1)
                return await self._upsert_user_internal(
                    db=db,
                    sub=sub,
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    phone_number_idp=phone_number_idp,
                    company_name=company_name,
                    retries_left=retries_left - 1,
                )
            else:
                raise
        except Exception as e:
            logger.error(f"Failed to upsert user {sub}: {e}")
            raise

    async def upsert_user(
        self,
        db: AsyncSession,
        sub: str,
        email: str,
        first_name: str,
        last_name: str,
        phone_number_idp: str | None = None,
        company_name: str | None = None,
        is_service_account: bool = False,
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
            phone_number_idp: The user's phone number from IDP (optional)
            company_name: The user's company name (optional)
            is_service_account: True when the caller is a machine identity rather than a
                person. Monotonic rather than insert-only: a machine account onboarded
                before migration 088 — and missed by its email-pattern backfill, which a
                client-credentials token carrying no email would be — corrects itself on
                its next call. Never cleared, so a person is not re-classified by a token
                that happens to lack the claim.

        Returns:
            The created or updated user
        """

        try:
            return await self._upsert_user_internal(
                db=db,
                sub=sub,
                email=email,
                first_name=first_name,
                last_name=last_name,
                phone_number_idp=phone_number_idp,
                company_name=company_name,
                is_service_account=is_service_account,
                retries_left=1,
            )

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
