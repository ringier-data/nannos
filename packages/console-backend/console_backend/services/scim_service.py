"""SCIM 2.0 provisioning service.

Translates between SCIM protocol representations and internal user/group models.
Wraps existing UserService and UserGroupService for the actual database operations.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.scim import (
    SCIM_GROUP_SCHEMA,
    SCIM_USER_SCHEMA,
    ScimEmail,
    ScimError,
    ScimGroup,
    ScimGroupCreate,
    ScimGroupRef,
    ScimListResponse,
    ScimMember,
    ScimMeta,
    ScimName,
    ScimPatchOp,
    ScimUser,
    ScimUserCreate,
)
from ..models.user import User, UserRole, UserStatus

if TYPE_CHECKING:
    from .user_group_service import UserGroupService
    from .user_service import UserService

logger = logging.getLogger(__name__)

# Virtual actor used for all SCIM-sourced audit log entries.
# `actor_sub` in audit_logs is a plain TEXT column with no FK constraint,
# so using a synthetic sub is safe.
SCIM_ACTOR = User(
    id="scim-system",
    sub="scim",
    email="",
    first_name="SCIM",
    last_name="Provisioner",
    role=UserRole.ADMIN,
    status=UserStatus.ACTIVE,
)


class ScimException(Exception):
    """SCIM protocol error with HTTP status and SCIM error body."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        super().__init__(detail)

    def to_scim_error(self) -> ScimError:
        return ScimError(detail=self.detail, status=str(self.status), scimType=self.scim_type)


# ─── Filter Parsing ──────────────────────────────────────────────────────────

# Minimal SCIM filter parser supporting: attribute eq "value"
_FILTER_PATTERN = re.compile(
    r'^(\w+(?:\.\w+)?)\s+eq\s+"([^"]*)"$',
    re.IGNORECASE,
)


def parse_scim_filter(filter_str: str | None) -> tuple[str, str] | None:
    """Parse a simple SCIM filter expression.

    Supports only: attribute eq "value"

    Returns (attribute, value) tuple or None if no filter.
    Raises ScimException for unsupported filter syntax.
    """
    if not filter_str:
        return None

    match = _FILTER_PATTERN.match(filter_str.strip())
    if not match:
        raise ScimException(
            status=400,
            detail=f"Unsupported filter syntax: {filter_str}. Only 'attribute eq \"value\"' is supported.",
            scim_type="invalidFilter",
        )

    return match.group(1).lower(), match.group(2)


# ─── User Service ────────────────────────────────────────────────────────────


class ScimUserService:
    """SCIM user provisioning operations.

    Delegates to ``UserService`` for write operations on the ``users`` table
    so that audit logging and any other cross-cutting concerns remain
    centralised.  SCIM-specific fields (``scim_external_id``,
    ``scim_user_name``) that are not part of the core ``User`` model are still
    managed with targeted SQL here.
    """

    def __init__(self) -> None:
        self._user_service: UserService | None = None

    def set_user_service(self, user_service: UserService) -> None:
        """Inject the shared UserService instance."""
        self._user_service = user_service

    @property
    def user_service(self) -> UserService:
        if self._user_service is None:
            raise RuntimeError("UserService not injected into ScimUserService. Call set_user_service().")
        return self._user_service

    async def create_user(self, db: AsyncSession, data: ScimUserCreate, *, base_url: str) -> ScimUser:
        """Create a new user from SCIM request."""
        # Check for duplicate externalId
        if data.externalId:
            existing = await self._get_user_by_external_id(db, data.externalId)
            if existing:
                raise ScimException(
                    status=409,
                    detail=f"User with externalId '{data.externalId}' already exists",
                    scim_type="uniqueness",
                )

        # Resolve the email: prefer emails[].value (primary first), fall back to userName
        email = self._resolve_email(data)

        # Check for duplicate email/userName
        existing_email = await self._get_user_by_email(db, email)
        if existing_email:
            raise ScimException(
                status=409,
                detail=f"User with userName '{data.userName}' already exists",
                scim_type="uniqueness",
            )

        user_id = str(uuid4())
        now = datetime.now(timezone.utc)

        first_name = data.name.givenName if data.name else ""
        last_name = data.name.familyName if data.name else ""

        await db.execute(
            text("""
                INSERT INTO users (id, sub, email, first_name, last_name, role, status,
                                   is_administrator, scim_external_id, scim_user_name, created_at, updated_at)
                VALUES (:id, :sub, :email, :first_name, :last_name, 'member', 'active',
                        false, :scim_external_id, :scim_user_name, :now, :now)
            """),
            {
                "id": user_id,
                "sub": user_id,  # Use ID as sub placeholder until OIDC login
                "email": email,
                "first_name": first_name or "",
                "last_name": last_name or "",
                "scim_external_id": data.externalId,
                "scim_user_name": data.userName,
                "now": now,
            },
        )

        return self._build_scim_user(
            id=user_id,
            email=email,
            userName=data.userName,
            first_name=first_name or "",
            last_name=last_name or "",
            external_id=data.externalId,
            active=True,
            created_at=now,
            updated_at=now,
            base_url=base_url,
        )

    async def get_user(self, db: AsyncSession, user_id: str, *, base_url: str) -> ScimUser:
        """Get a single user by internal ID."""
        row = await self._fetch_user_row(db, user_id)
        if not row:
            raise ScimException(status=404, detail=f"User '{user_id}' not found")
        return self._row_to_scim_user(row, base_url=base_url)

    async def list_users(
        self, db: AsyncSession, *, filter_str: str | None, start_index: int, count: int,
        sort_by: str | None = None, sort_order: str = "ascending", base_url: str,
    ) -> ScimListResponse:
        """List users with optional filtering and sorting."""
        parsed = parse_scim_filter(filter_str)
        order_clause = self._resolve_sort(sort_by, sort_order)

        if parsed:
            attr, value = parsed
            if attr in ("username", "emails.value"):
                rows, total = await self._query_users_by_email(db, value, start_index, count, order_clause)
            elif attr == "externalid":
                rows, total = await self._query_users_by_external_id(db, value, start_index, count, order_clause)
            else:
                raise ScimException(
                    status=400,
                    detail=f"Filtering on '{attr}' is not supported. Supported: userName, externalId, emails.value",
                    scim_type="invalidFilter",
                )
        else:
            rows, total = await self._query_users_all(db, start_index, count, order_clause)

        resources = [self._row_to_scim_user(r, base_url=base_url).model_dump(exclude_none=True, by_alias=True) for r in rows]

        return ScimListResponse(
            totalResults=total,
            startIndex=start_index,
            itemsPerPage=len(resources),
            Resources=resources,
        )

    async def replace_user(self, db: AsyncSession, user_id: str, data: ScimUserCreate, *, base_url: str) -> ScimUser:
        """Full replacement of a user (PUT)."""
        existing = await self._fetch_user_row(db, user_id)
        if not existing:
            raise ScimException(status=404, detail=f"User '{user_id}' not found")

        email = self._resolve_email(data)
        first_name = data.name.givenName if data.name else ""
        last_name = data.name.familyName if data.name else ""
        status = "active" if data.active else "suspended"
        now = datetime.now(timezone.utc)

        await db.execute(
            text("""
                UPDATE users
                SET email = :email, first_name = :first_name, last_name = :last_name,
                    status = :status, scim_external_id = :scim_external_id,
                    scim_user_name = :scim_user_name, updated_at = :now
                WHERE id = :id AND deleted_at IS NULL
            """),
            {
                "id": user_id,
                "email": email,
                "first_name": first_name or "",
                "last_name": last_name or "",
                "status": status,
                "scim_external_id": data.externalId,
                "scim_user_name": data.userName,
                "now": now,
            },
        )

        return self._build_scim_user(
            id=user_id,
            email=email,
            userName=data.userName,
            first_name=first_name or "",
            last_name=last_name or "",
            external_id=data.externalId,
            active=data.active,
            created_at=existing.created_at,
            updated_at=now,
            base_url=base_url,
        )

    async def patch_user(self, db: AsyncSession, user_id: str, patch: ScimPatchOp, *, base_url: str) -> ScimUser:
        """Partial update of a user (PATCH)."""
        existing = await self._fetch_user_row(db, user_id)
        if not existing:
            raise ScimException(status=404, detail=f"User '{user_id}' not found")

        updates: dict[str, str] = {}
        now = datetime.now(timezone.utc)

        for op in patch.Operations:
            if op.op == "replace":
                if op.path == "active" or (op.path is None and isinstance(op.value, dict) and "active" in op.value):
                    active = op.value if op.path == "active" else op.value["active"]
                    updates["status"] = "active" if active else "suspended"
                elif op.path == "userName" or (op.path is None and isinstance(op.value, dict) and "userName" in op.value):
                    user_name = op.value if op.path == "userName" else op.value["userName"]
                    updates["scim_user_name"] = user_name
                elif op.path == "name.givenName":
                    updates["first_name"] = op.value
                elif op.path == "name.familyName":
                    updates["last_name"] = op.value
                elif op.path == "externalId":
                    updates["scim_external_id"] = op.value
                elif op.path == "emails" or (op.path is None and isinstance(op.value, dict) and "emails" in op.value):
                    emails = op.value if op.path == "emails" else op.value["emails"]
                    if emails and isinstance(emails, list):
                        updates["email"] = emails[0]["value"] if isinstance(emails[0], dict) else emails[0]
                elif op.path is None and isinstance(op.value, dict):
                    # Bulk replace with dict
                    if "name" in op.value:
                        name = op.value["name"]
                        if "givenName" in name:
                            updates["first_name"] = name["givenName"]
                        if "familyName" in name:
                            updates["last_name"] = name["familyName"]

        if updates:
            set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
            updates["id"] = user_id
            updates["now"] = now.isoformat()
            await db.execute(
                text(f"UPDATE users SET {set_clauses}, updated_at = :now WHERE id = :id AND deleted_at IS NULL"),
                {**updates, "now": now},
            )

        return await self.get_user(db, user_id, base_url=base_url)

    async def delete_user(self, db: AsyncSession, user_id: str) -> None:
        """Soft-delete a user via UserService (ensures audit log and consistent status handling)."""
        # Pre-check: ensure the user exists and is not already deleted, mirroring the original
        # behaviour where the UPDATE's rowcount returned 0 for missing / already-deleted rows.
        row = await self._fetch_user_row(db, user_id)
        if not row:
            raise ScimException(status=404, detail=f"User '{user_id}' not found")
        await self.user_service.update_user_status(db, user_id, SCIM_ACTOR, UserStatus.DELETED)

    # ─── Internal helpers ─────────────────────────────────────────────────────

    # Mapping of SCIM sortBy attribute names to SQL column names
    _SORT_COLUMNS = {
        "username": "email",
        "name.givenname": "first_name",
        "name.familyname": "last_name",
        "emails.value": "email",
        "externalid": "scim_external_id",
        "meta.created": "created_at",
        "meta.lastmodified": "updated_at",
    }

    def _resolve_sort(self, sort_by: str | None, sort_order: str) -> str:
        """Resolve SCIM sortBy/sortOrder to an SQL ORDER BY clause."""
        if not sort_by:
            return "ORDER BY created_at"
        column = self._SORT_COLUMNS.get(sort_by.lower())
        if not column:
            raise ScimException(
                status=400,
                detail=f"sortBy '{sort_by}' is not supported.",
                scim_type="invalidValue",
            )
        direction = "DESC" if sort_order.lower() == "descending" else "ASC"
        return f"ORDER BY {column} {direction}"

    @staticmethod
    def _resolve_email(data: ScimUserCreate) -> str:
        """Extract email from SCIM request: prefer emails[].value (primary first), fall back to userName."""
        if data.emails:
            # Prefer the primary email
            for em in data.emails:
                if em.primary:
                    return em.value
            # Otherwise use the first email
            return data.emails[0].value
        return data.userName

    async def _fetch_user_row(self, db: AsyncSession, user_id: str):
        result = await db.execute(
            text("""
                SELECT id, sub, email, first_name, last_name, status,
                       scim_external_id, scim_user_name, created_at, updated_at
                FROM users
                WHERE id = :id AND deleted_at IS NULL
            """),
            {"id": user_id},
        )
        return result.fetchone()

    async def _get_user_by_external_id(self, db: AsyncSession, external_id: str):
        result = await db.execute(
            text("SELECT id FROM users WHERE scim_external_id = :eid AND deleted_at IS NULL"),
            {"eid": external_id},
        )
        return result.fetchone()

    async def _get_user_by_email(self, db: AsyncSession, email: str):
        result = await db.execute(
            text("SELECT id FROM users WHERE email = :email AND deleted_at IS NULL"),
            {"email": email},
        )
        return result.fetchone()

    async def _query_users_by_email(self, db: AsyncSession, email: str, start_index: int, count: int, order_clause: str = "ORDER BY created_at"):
        total_result = await db.execute(
            text("SELECT COUNT(*) FROM users WHERE email = :email AND deleted_at IS NULL"),
            {"email": email},
        )
        total = total_result.scalar() or 0

        result = await db.execute(
            text(f"""
                SELECT id, sub, email, first_name, last_name, status,
                       scim_external_id, scim_user_name, created_at, updated_at
                FROM users WHERE email = :email AND deleted_at IS NULL
                {order_clause} LIMIT :count OFFSET :offset
            """),
            {"email": email, "count": count, "offset": start_index - 1},
        )
        return result.fetchall(), total

    async def _query_users_by_external_id(self, db: AsyncSession, external_id: str, start_index: int, count: int, order_clause: str = "ORDER BY created_at"):
        total_result = await db.execute(
            text("SELECT COUNT(*) FROM users WHERE scim_external_id = :eid AND deleted_at IS NULL"),
            {"eid": external_id},
        )
        total = total_result.scalar() or 0

        result = await db.execute(
            text(f"""
                SELECT id, sub, email, first_name, last_name, status,
                       scim_external_id, scim_user_name, created_at, updated_at
                FROM users WHERE scim_external_id = :eid AND deleted_at IS NULL
                {order_clause} LIMIT :count OFFSET :offset
            """),
            {"eid": external_id, "count": count, "offset": start_index - 1},
        )
        return result.fetchall(), total

    async def _query_users_all(self, db: AsyncSession, start_index: int, count: int, order_clause: str = "ORDER BY created_at"):
        total_result = await db.execute(
            text("SELECT COUNT(*) FROM users WHERE deleted_at IS NULL")
        )
        total = total_result.scalar() or 0

        result = await db.execute(
            text(f"""
                SELECT id, sub, email, first_name, last_name, status,
                       scim_external_id, scim_user_name, created_at, updated_at
                FROM users WHERE deleted_at IS NULL
                {order_clause} LIMIT :count OFFSET :offset
            """),
            {"count": count, "offset": start_index - 1},
        )
        return result.fetchall(), total

    def _row_to_scim_user(self, row, *, base_url: str) -> ScimUser:
        return self._build_scim_user(
            id=row.id,
            email=row.email,
            userName=row.scim_user_name,
            first_name=row.first_name,
            last_name=row.last_name,
            external_id=row.scim_external_id,
            active=row.status == "active",
            created_at=row.created_at,
            updated_at=row.updated_at,
            base_url=base_url,
        )

    def _build_scim_user(
        self,
        *,
        id: str,
        email: str,
        userName: str | None = None,
        first_name: str,
        last_name: str,
        external_id: str | None,
        active: bool,
        created_at: datetime,
        updated_at: datetime,
        base_url: str,
    ) -> ScimUser:
        # userName: use stored SCIM userName if available, otherwise fall back to email
        resolved_user_name = userName if userName else email
        return ScimUser(
            id=id,
            externalId=external_id,
            userName=resolved_user_name,
            name=ScimName(givenName=first_name, familyName=last_name, formatted=f"{first_name} {last_name}".strip()),
            displayName=f"{first_name} {last_name}".strip() or resolved_user_name,
            emails=[ScimEmail(value=email, type="work", primary=True)],
            active=active,
            meta=ScimMeta(
                resourceType="User",
                created=created_at,
                lastModified=updated_at,
                location=f"{base_url}/api/scim/v2/Users/{id}",
            ),
        )


# ─── Group Service ───────────────────────────────────────────────────────────


class ScimGroupService:
    """SCIM group provisioning operations.

    Delegates to ``UserGroupService`` for write operations on the
    ``user_groups`` and ``user_group_members`` tables so that Keycloak
    synchronisation, audit logging, notification dispatch, and agent-activation
    side-effects are handled consistently.  SCIM-specific fields
    (``scim_external_id``) that are not part of the core group model are still
    managed with targeted SQL here.
    """

    def __init__(self) -> None:
        self._user_group_service: UserGroupService | None = None

    def set_user_group_service(self, user_group_service: UserGroupService) -> None:
        """Inject the shared UserGroupService instance."""
        self._user_group_service = user_group_service

    @property
    def user_group_service(self) -> UserGroupService:
        if self._user_group_service is None:
            raise RuntimeError(
                "UserGroupService not injected into ScimGroupService. Call set_user_group_service()."
            )
        return self._user_group_service

    async def create_group(self, db: AsyncSession, data: ScimGroupCreate, *, base_url: str) -> ScimGroup:
        """Create a new group from SCIM request."""
        if data.externalId:
            existing = await self._get_group_by_external_id(db, data.externalId)
            if existing:
                raise ScimException(
                    status=409,
                    detail=f"Group with externalId '{data.externalId}' already exists",
                    scim_type="uniqueness",
                )

        # Check for duplicate displayName
        existing_name = await db.execute(
            text("SELECT id FROM user_groups WHERE name = :name AND deleted_at IS NULL"),
            {"name": data.displayName},
        )
        if existing_name.scalar():
            raise ScimException(
                status=409,
                detail=f"Group with displayName '{data.displayName}' already exists",
                scim_type="uniqueness",
            )

        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                INSERT INTO user_groups (name, scim_external_id, created_at, updated_at)
                VALUES (:name, :scim_external_id, :now, :now)
                RETURNING id
            """),
            {"name": data.displayName, "scim_external_id": data.externalId, "now": now},
        )
        group_id = result.scalar()

        # Add members if provided
        if data.members:
            for member in data.members:
                await self._add_member(db, group_id, member.value)

        members = await self._get_group_members(db, group_id)

        return ScimGroup(
            id=str(group_id),
            externalId=data.externalId,
            displayName=data.displayName,
            members=members,
            meta=ScimMeta(
                resourceType="Group",
                created=now,
                lastModified=now,
                location=f"{base_url}/scim/v2/Groups/{group_id}",
            ),
        )

    async def get_group(self, db: AsyncSession, group_id: str, *, base_url: str) -> ScimGroup:
        """Get a single group by ID."""
        row = await self._fetch_group_row(db, group_id)
        if not row:
            raise ScimException(status=404, detail=f"Group '{group_id}' not found")

        members = await self._get_group_members(db, int(group_id))
        return self._row_to_scim_group(row, members=members, base_url=base_url)

    async def list_groups(
        self, db: AsyncSession, *, filter_str: str | None, start_index: int, count: int, base_url: str
    ) -> ScimListResponse:
        """List groups with optional filtering."""
        parsed = parse_scim_filter(filter_str)

        if parsed:
            attr, value = parsed
            if attr == "displayname":
                rows, total = await self._query_groups_by_name(db, value, start_index, count)
            elif attr == "externalid":
                rows, total = await self._query_groups_by_external_id(db, value, start_index, count)
            else:
                raise ScimException(
                    status=400,
                    detail=f"Filtering on '{attr}' is not supported. Supported: displayName, externalId",
                    scim_type="invalidFilter",
                )
        else:
            rows, total = await self._query_groups_all(db, start_index, count)

        resources = []
        for r in rows:
            members = await self._get_group_members(db, r.id)
            group = self._row_to_scim_group(r, members=members, base_url=base_url)
            resources.append(group.model_dump(exclude_none=True, by_alias=True))

        return ScimListResponse(
            totalResults=total,
            startIndex=start_index,
            itemsPerPage=len(resources),
            Resources=resources,
        )

    async def replace_group(self, db: AsyncSession, group_id: str, data: ScimGroupCreate, *, base_url: str) -> ScimGroup:
        """Full replacement of a group (PUT)."""
        existing = await self._fetch_group_row(db, group_id)
        if not existing:
            raise ScimException(status=404, detail=f"Group '{group_id}' not found")

        now = datetime.now(timezone.utc)
        gid = int(group_id)

        await db.execute(
            text("""
                UPDATE user_groups
                SET name = :name, scim_external_id = :scim_external_id, updated_at = :now
                WHERE id = :id AND deleted_at IS NULL
            """),
            {"id": gid, "name": data.displayName, "scim_external_id": data.externalId, "now": now},
        )

        # Reconcile membership via diff — delegates removes/adds to UserGroupService
        # so that Keycloak sync, audit, notifications, and activation cleanup are applied.
        requested_ids = {m.value for m in data.members} if data.members else set()
        current_ids = await self._get_current_member_ids(db, gid)
        to_remove = list(current_ids - requested_ids)
        to_add = list(requested_ids - current_ids)

        if to_remove:
            try:
                await self.user_group_service.remove_members(db, SCIM_ACTOR, gid, to_remove)
            except Exception as exc:
                logger.warning("SCIM replace_group: remove_members failed for group %s: %s", gid, exc)

        for user_id in to_add:
            await self._add_member(db, gid, user_id)

        members = await self._get_group_members(db, gid)
        return ScimGroup(
            id=group_id,
            externalId=data.externalId,
            displayName=data.displayName,
            members=members,
            meta=ScimMeta(
                resourceType="Group",
                created=existing.created_at,
                lastModified=now,
                location=f"{base_url}/scim/v2/Groups/{group_id}",
            ),
        )

    async def patch_group(self, db: AsyncSession, group_id: str, patch: ScimPatchOp, *, base_url: str) -> ScimGroup:
        """Partial update of a group (PATCH)."""
        existing = await self._fetch_group_row(db, group_id)
        if not existing:
            raise ScimException(status=404, detail=f"Group '{group_id}' not found")

        gid = int(group_id)
        now = datetime.now(timezone.utc)

        for op in patch.Operations:
            if op.op == "add" and op.path == "members":
                members_to_add = op.value if isinstance(op.value, list) else [op.value]
                for m in members_to_add:
                    user_id = m["value"] if isinstance(m, dict) else m
                    await self._add_member(db, gid, user_id)

            elif op.op == "remove" and op.path and op.path.startswith("members"):
                # Handle: members[value eq "user-id"]
                match = re.search(r'members\[value\s+eq\s+"([^"]+)"\]', op.path)
                if match:
                    user_id = match.group(1)
                    try:
                        await self.user_group_service.remove_members(db, SCIM_ACTOR, gid, [user_id])
                    except Exception as exc:
                        logger.warning(
                            "SCIM patch_group: remove_members failed for group %s, user %s: %s",
                            gid, user_id, exc,
                        )
                elif op.path == "members" and op.value:
                    # Handle: path=members with value=[{"value": "user-id"}]
                    members_to_remove = op.value if isinstance(op.value, list) else [op.value]
                    ids_to_remove = [m["value"] if isinstance(m, dict) else m for m in members_to_remove]
                    try:
                        await self.user_group_service.remove_members(db, SCIM_ACTOR, gid, ids_to_remove)
                    except Exception as exc:
                        logger.warning(
                            "SCIM patch_group: remove_members failed for group %s: %s", gid, exc
                        )

            elif op.op == "replace" and op.path == "displayName":
                await db.execute(
                    text("UPDATE user_groups SET name = :name, updated_at = :now WHERE id = :id"),
                    {"id": gid, "name": op.value, "now": now},
                )

            elif op.op == "replace" and op.path == "members":
                # Full member replacement using diff — same approach as replace_group
                members_list = (op.value if isinstance(op.value, list) else [op.value]) if op.value else []
                requested_ids = {m["value"] if isinstance(m, dict) else m for m in members_list}
                current_ids = await self._get_current_member_ids(db, gid)
                to_remove = list(current_ids - requested_ids)
                to_add = list(requested_ids - current_ids)
                if to_remove:
                    try:
                        await self.user_group_service.remove_members(db, SCIM_ACTOR, gid, to_remove)
                    except Exception as exc:
                        logger.warning(
                            "SCIM patch_group replace members: remove_members failed for group %s: %s",
                            gid, exc,
                        )
                for user_id in to_add:
                    await self._add_member(db, gid, user_id)

        await db.execute(
            text("UPDATE user_groups SET updated_at = :now WHERE id = :id"),
            {"id": gid, "now": now},
        )

        return await self.get_group(db, group_id, base_url=base_url)

    async def delete_group(self, db: AsyncSession, group_id: str) -> None:
        """Soft-delete a group via UserGroupService (ensures Keycloak sync, audit, and activation cleanup)."""
        gid = self._parse_group_id(group_id)
        # force=True so that SCIM can delete groups that still have sub-agents assigned,
        # mirroring the authoritative nature of the identity provider.
        deleted = await self.user_group_service.delete_group(db, SCIM_ACTOR, gid, force=True)
        if not deleted:
            raise ScimException(status=404, detail=f"Group '{group_id}' not found")

    # ─── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_group_id(group_id: str) -> int:
        """Parse and validate a group ID string as a valid int32.

        Raises ScimException(404) if the ID is not a valid integer or out of int32 range.
        """
        try:
            gid = int(group_id)
        except (ValueError, OverflowError):
            raise ScimException(status=404, detail=f"Group '{group_id}' not found")
        if gid < -2147483648 or gid > 2147483647:
            raise ScimException(status=404, detail=f"Group '{group_id}' not found")
        return gid

    async def _fetch_group_row(self, db: AsyncSession, group_id: str):
        gid = self._parse_group_id(group_id)
        result = await db.execute(
            text("""
                SELECT id, name, scim_external_id, created_at, updated_at
                FROM user_groups
                WHERE id = :id AND deleted_at IS NULL
            """),
            {"id": gid},
        )
        return result.fetchone()

    async def _get_group_by_external_id(self, db: AsyncSession, external_id: str):
        result = await db.execute(
            text("SELECT id FROM user_groups WHERE scim_external_id = :eid AND deleted_at IS NULL"),
            {"eid": external_id},
        )
        return result.fetchone()

    async def _get_group_members(self, db: AsyncSession, group_id: int) -> list[ScimMember]:
        result = await db.execute(
            text("""
                SELECT u.id, u.email, u.first_name, u.last_name
                FROM user_group_members ugm
                JOIN users u ON u.id = ugm.user_id
                WHERE ugm.user_group_id = :gid AND u.deleted_at IS NULL
            """),
            {"gid": group_id},
        )
        rows = result.fetchall()
        return [
            ScimMember(
                value=row.id,
                display=f"{row.first_name} {row.last_name}".strip() or row.email,
            )
            for row in rows
        ]

    async def _add_member(self, db: AsyncSession, group_id: int, user_id: str) -> None:
        """Add a member to a group via UserGroupService (handles Keycloak sync and agent activation)."""
        try:
            await self.user_group_service.add_member(db, SCIM_ACTOR, group_id, user_id, role="write")
        except Exception as exc:
            # Log but swallow so that a single bad member reference doesn't abort the whole operation.
            logger.warning("SCIM _add_member(%s, %s) failed: %s", group_id, user_id, exc)

    async def _get_current_member_ids(self, db: AsyncSession, group_id: int) -> set[str]:
        """Return the set of user IDs currently in the group (all non-deleted users)."""
        result = await db.execute(
            text("""
                SELECT ugm.user_id
                FROM user_group_members ugm
                JOIN users u ON u.id = ugm.user_id
                WHERE ugm.user_group_id = :gid AND u.deleted_at IS NULL
            """),
            {"gid": group_id},
        )
        return {row[0] for row in result.fetchall()}


    async def _query_groups_by_name(self, db: AsyncSession, name: str, start_index: int, count: int):
        total_result = await db.execute(
            text("SELECT COUNT(*) FROM user_groups WHERE name = :name AND deleted_at IS NULL"),
            {"name": name},
        )
        total = total_result.scalar() or 0
        result = await db.execute(
            text("""
                SELECT id, name, scim_external_id, created_at, updated_at
                FROM user_groups WHERE name = :name AND deleted_at IS NULL
                ORDER BY created_at LIMIT :count OFFSET :offset
            """),
            {"name": name, "count": count, "offset": start_index - 1},
        )
        return result.fetchall(), total

    async def _query_groups_by_external_id(self, db: AsyncSession, external_id: str, start_index: int, count: int):
        total_result = await db.execute(
            text("SELECT COUNT(*) FROM user_groups WHERE scim_external_id = :eid AND deleted_at IS NULL"),
            {"eid": external_id},
        )
        total = total_result.scalar() or 0
        result = await db.execute(
            text("""
                SELECT id, name, scim_external_id, created_at, updated_at
                FROM user_groups WHERE scim_external_id = :eid AND deleted_at IS NULL
                ORDER BY created_at LIMIT :count OFFSET :offset
            """),
            {"eid": external_id, "count": count, "offset": start_index - 1},
        )
        return result.fetchall(), total

    async def _query_groups_all(self, db: AsyncSession, start_index: int, count: int):
        total_result = await db.execute(
            text("SELECT COUNT(*) FROM user_groups WHERE deleted_at IS NULL")
        )
        total = total_result.scalar() or 0
        result = await db.execute(
            text("""
                SELECT id, name, scim_external_id, created_at, updated_at
                FROM user_groups WHERE deleted_at IS NULL
                ORDER BY created_at LIMIT :count OFFSET :offset
            """),
            {"count": count, "offset": start_index - 1},
        )
        return result.fetchall(), total

    def _row_to_scim_group(self, row, *, members: list[ScimMember], base_url: str) -> ScimGroup:
        return ScimGroup(
            id=str(row.id),
            externalId=row.scim_external_id,
            displayName=row.name,
            members=members if members else None,
            meta=ScimMeta(
                resourceType="Group",
                created=row.created_at,
                lastModified=row.updated_at,
                location=f"{base_url}/scim/v2/Groups/{row.id}",
            ),
        )
