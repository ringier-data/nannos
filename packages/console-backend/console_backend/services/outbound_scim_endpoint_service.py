"""Service for managing outbound SCIM endpoint configuration."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.outbound_scim import OutboundScimEndpoint, OutboundScimEndpointCreated
from ..models.user import User
from ..services.audit_service import AuditService

logger = logging.getLogger(__name__)


class OutboundScimEndpointService:
    """Manages outbound SCIM endpoint lifecycle (create, list, update, delete)."""

    def __init__(self) -> None:
        self._audit_service: AuditService | None = None

    def set_audit_service(self, audit_service: AuditService) -> None:
        self._audit_service = audit_service

    @property
    def audit_service(self) -> AuditService:
        if self._audit_service is None:
            raise RuntimeError("AuditService not set on OutboundScimEndpointService")
        return self._audit_service

    async def create_endpoint(
        self,
        db: AsyncSession,
        *,
        name: str,
        endpoint_url: str,
        bearer_token: str,
        push_users: bool,
        push_groups: bool,
        actor: User,
    ) -> OutboundScimEndpointCreated:
        """Create a new outbound SCIM endpoint."""
        result = await db.execute(
            text("""
                INSERT INTO outbound_scim_endpoints
                    (name, endpoint_url, bearer_token, push_users, push_groups, created_by)
                VALUES (:name, :endpoint_url, :bearer_token, :push_users, :push_groups, :created_by)
                RETURNING id, enabled, created_at
            """),
            {
                "name": name,
                "endpoint_url": endpoint_url,
                "bearer_token": bearer_token,
                "push_users": push_users,
                "push_groups": push_groups,
                "created_by": actor.id,
            },
        )
        row = result.fetchone()

        await self.audit_service.log_action(
            db,
            actor=actor,
            entity_type=AuditEntityType.OUTBOUND_SCIM_ENDPOINT,
            entity_id=str(row.id),
            action=AuditAction.CREATE,
            changes={"after": {"name": name, "endpoint_url": endpoint_url, "push_users": push_users, "push_groups": push_groups}},
        )

        return OutboundScimEndpointCreated(
            id=row.id,
            name=name,
            endpoint_url=endpoint_url,
            bearer_token=bearer_token,
            enabled=row.enabled,
            push_users=push_users,
            push_groups=push_groups,
            created_at=row.created_at,
        )

    async def list_endpoints(self, db: AsyncSession) -> list[OutboundScimEndpoint]:
        """List all active (non-deleted) outbound SCIM endpoints."""
        result = await db.execute(
            text("""
                SELECT id, name, endpoint_url, bearer_token, enabled,
                       push_users, push_groups, created_by, created_at, updated_at
                FROM outbound_scim_endpoints
                WHERE deleted_at IS NULL
                ORDER BY created_at DESC
            """)
        )
        rows = result.fetchall()
        return [
            OutboundScimEndpoint(
                id=row.id,
                name=row.name,
                endpoint_url=row.endpoint_url,
                token_hint=row.bearer_token[-4:],
                enabled=row.enabled,
                push_users=row.push_users,
                push_groups=row.push_groups,
                created_by=row.created_by,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    async def get_endpoint(self, db: AsyncSession, endpoint_id: int) -> OutboundScimEndpoint | None:
        """Get a single endpoint by ID (masked token)."""
        result = await db.execute(
            text("""
                SELECT id, name, endpoint_url, bearer_token, enabled,
                       push_users, push_groups, created_by, created_at, updated_at
                FROM outbound_scim_endpoints
                WHERE id = :id AND deleted_at IS NULL
            """),
            {"id": endpoint_id},
        )
        row = result.fetchone()
        if not row:
            return None

        return OutboundScimEndpoint(
            id=row.id,
            name=row.name,
            endpoint_url=row.endpoint_url,
            token_hint=row.bearer_token[-4:],
            enabled=row.enabled,
            push_users=row.push_users,
            push_groups=row.push_groups,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def update_endpoint(
        self,
        db: AsyncSession,
        endpoint_id: int,
        *,
        actor: User,
        name: str | None = None,
        endpoint_url: str | None = None,
        bearer_token: str | None = None,
        enabled: bool | None = None,
        push_users: bool | None = None,
        push_groups: bool | None = None,
    ) -> OutboundScimEndpoint | None:
        """Update an outbound SCIM endpoint."""
        # Build dynamic SET clause
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if endpoint_url is not None:
            fields["endpoint_url"] = endpoint_url
        if bearer_token is not None:
            fields["bearer_token"] = bearer_token
        if enabled is not None:
            fields["enabled"] = enabled
        if push_users is not None:
            fields["push_users"] = push_users
        if push_groups is not None:
            fields["push_groups"] = push_groups

        if not fields:
            return await self.get_endpoint(db, endpoint_id)

        fields["updated_at"] = text("NOW()")

        set_clauses = []
        params: dict[str, object] = {"id": endpoint_id}
        for key, value in fields.items():
            if key == "updated_at":
                set_clauses.append("updated_at = NOW()")
            else:
                set_clauses.append(f"{key} = :{key}")
                params[key] = value

        query = f"""
            UPDATE outbound_scim_endpoints
            SET {', '.join(set_clauses)}
            WHERE id = :id AND deleted_at IS NULL
            RETURNING id
        """

        result = await db.execute(text(query), params)
        row = result.fetchone()
        if not row:
            return None

        # Log changes (mask token in audit)
        audit_changes: dict[str, object] = {k: v for k, v in fields.items() if k not in ("updated_at", "bearer_token")}
        if bearer_token is not None:
            audit_changes["bearer_token"] = "***updated***"

        await self.audit_service.log_action(
            db,
            actor=actor,
            entity_type=AuditEntityType.OUTBOUND_SCIM_ENDPOINT,
            entity_id=str(endpoint_id),
            action=AuditAction.UPDATE,
            changes={"after": audit_changes},
        )

        return await self.get_endpoint(db, endpoint_id)

    async def delete_endpoint(self, db: AsyncSession, endpoint_id: int, *, actor: User) -> bool:
        """Soft-delete an outbound SCIM endpoint."""
        result = await db.execute(
            text("""
                UPDATE outbound_scim_endpoints
                SET deleted_at = NOW(), updated_at = NOW()
                WHERE id = :id AND deleted_at IS NULL
                RETURNING id
            """),
            {"id": endpoint_id},
        )
        row = result.fetchone()
        if not row:
            return False

        await self.audit_service.log_action(
            db,
            actor=actor,
            entity_type=AuditEntityType.OUTBOUND_SCIM_ENDPOINT,
            entity_id=str(endpoint_id),
            action=AuditAction.DELETE,
            changes={},
        )

        return True

    async def get_active_endpoints(self, db: AsyncSession) -> list[dict]:
        """Get all active endpoints with full bearer tokens (for push service use only)."""
        result = await db.execute(
            text("""
                SELECT id, endpoint_url, bearer_token, push_users, push_groups
                FROM outbound_scim_endpoints
                WHERE deleted_at IS NULL AND enabled = true
            """)
        )
        rows = result.fetchall()
        return [
            {
                "id": row.id,
                "endpoint_url": row.endpoint_url,
                "bearer_token": row.bearer_token,
                "push_users": row.push_users,
                "push_groups": row.push_groups,
            }
            for row in rows
        ]

    async def get_sync_state(
        self, db: AsyncSession, endpoint_id: int, entity_type: str, entity_id: str
    ) -> dict | None:
        """Get sync state for a specific entity at a specific endpoint."""
        result = await db.execute(
            text("""
                SELECT remote_id, last_synced_at, last_error, retry_count
                FROM outbound_scim_sync_state
                WHERE endpoint_id = :endpoint_id AND entity_type = :entity_type AND entity_id = :entity_id
            """),
            {"endpoint_id": endpoint_id, "entity_type": entity_type, "entity_id": entity_id},
        )
        row = result.fetchone()
        if not row:
            return None
        return {
            "remote_id": row.remote_id,
            "last_synced_at": row.last_synced_at,
            "last_error": row.last_error,
            "retry_count": row.retry_count,
        }

    async def upsert_sync_state(
        self,
        db: AsyncSession,
        *,
        endpoint_id: int,
        entity_type: str,
        entity_id: str,
        remote_id: str | None = None,
        last_error: str | None = None,
        increment_retry: bool = False,
    ) -> None:
        """Create or update sync state for an entity at an endpoint."""
        if increment_retry:
            await db.execute(
                text("""
                    INSERT INTO outbound_scim_sync_state
                        (endpoint_id, entity_type, entity_id, remote_id, last_error, retry_count, updated_at)
                    VALUES (:endpoint_id, :entity_type, :entity_id, :remote_id, :last_error, 1, NOW())
                    ON CONFLICT (endpoint_id, entity_type, entity_id)
                    DO UPDATE SET
                        remote_id = COALESCE(:remote_id, outbound_scim_sync_state.remote_id),
                        last_error = :last_error,
                        retry_count = outbound_scim_sync_state.retry_count + 1,
                        updated_at = NOW()
                """),
                {
                    "endpoint_id": endpoint_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "remote_id": remote_id,
                    "last_error": last_error,
                },
            )
        else:
            await db.execute(
                text("""
                    INSERT INTO outbound_scim_sync_state
                        (endpoint_id, entity_type, entity_id, remote_id, last_synced_at, last_error, retry_count, updated_at)
                    VALUES (:endpoint_id, :entity_type, :entity_id, :remote_id, NOW(), NULL, 0, NOW())
                    ON CONFLICT (endpoint_id, entity_type, entity_id)
                    DO UPDATE SET
                        remote_id = COALESCE(:remote_id, outbound_scim_sync_state.remote_id),
                        last_synced_at = NOW(),
                        last_error = NULL,
                        retry_count = 0,
                        updated_at = NOW()
                """),
                {
                    "endpoint_id": endpoint_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "remote_id": remote_id,
                },
            )
