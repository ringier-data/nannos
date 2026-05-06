"""Service for managing SCIM bearer tokens."""

import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.scim_token import ScimToken, ScimTokenCreated
from ..models.user import User
from ..services.audit_service import AuditService

logger = logging.getLogger(__name__)


class ScimTokenService:
    """Manages SCIM bearer token lifecycle (create, list, validate, revoke)."""

    def __init__(self) -> None:
        self._audit_service: AuditService | None = None

    def set_audit_service(self, audit_service: AuditService) -> None:
        self._audit_service = audit_service

    @property
    def audit_service(self) -> AuditService:
        if self._audit_service is None:
            raise RuntimeError("AuditService not set on ScimTokenService")
        return self._audit_service

    async def create_token(
        self,
        db: AsyncSession,
        *,
        name: str,
        description: str | None,
        expires_at: datetime | None,
        actor: User,
    ) -> ScimTokenCreated:
        """Create a new SCIM bearer token.

        Generates a cryptographically secure random token and stores it in the database.
        The full token value is only returned at creation time.
        """
        token_value = secrets.token_urlsafe(48)

        result = await db.execute(
            text("""
                INSERT INTO scim_tokens (name, description, token, created_by, expires_at)
                VALUES (:name, :description, :token, :created_by, :expires_at)
                RETURNING id, created_at
            """),
            {
                "name": name,
                "description": description,
                "token": token_value,
                "created_by": actor.id,
                "expires_at": expires_at,
            },
        )
        row = result.fetchone()

        await self.audit_service.log_action(
            db,
            actor=actor,
            entity_type=AuditEntityType.SCIM_TOKEN,
            entity_id=str(row.id),
            action=AuditAction.CREATE,
            changes={"after": {"name": name, "expires_at": expires_at.isoformat() if expires_at else None}},
        )

        return ScimTokenCreated(
            id=row.id,
            name=name,
            description=description,
            token=token_value,
            expires_at=expires_at,
            created_at=row.created_at,
        )

    async def list_tokens(self, db: AsyncSession) -> list[ScimToken]:
        """List all SCIM tokens (active and revoked) with masked token values."""
        result = await db.execute(
            text("""
                SELECT id, name, description, token, created_by,
                       last_used_at, expires_at, revoked_at, created_at
                FROM scim_tokens
                ORDER BY created_at DESC
            """)
        )
        rows = result.fetchall()
        return [
            ScimToken(
                id=row.id,
                name=row.name,
                description=row.description,
                token_hint=row.token[-4:],
                created_by=row.created_by,
                last_used_at=row.last_used_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def get_token(self, db: AsyncSession, token_id: int) -> ScimToken | None:
        """Get a single SCIM token by ID (masked)."""
        result = await db.execute(
            text("""
                SELECT id, name, description, token, created_by,
                       last_used_at, expires_at, revoked_at, created_at
                FROM scim_tokens
                WHERE id = :id
            """),
            {"id": token_id},
        )
        row = result.fetchone()
        if not row:
            return None

        return ScimToken(
            id=row.id,
            name=row.name,
            description=row.description,
            token_hint=row.token[-4:],
            created_by=row.created_by,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            created_at=row.created_at,
        )

    async def revoke_token(self, db: AsyncSession, token_id: int, *, actor: User) -> ScimToken | None:
        """Revoke a SCIM token by setting revoked_at timestamp."""
        result = await db.execute(
            text("""
                UPDATE scim_tokens
                SET revoked_at = NOW()
                WHERE id = :id AND revoked_at IS NULL
                RETURNING id, name, description, token, created_by,
                          last_used_at, expires_at, revoked_at, created_at
            """),
            {"id": token_id},
        )
        row = result.fetchone()
        if not row:
            return None

        await self.audit_service.log_action(
            db,
            actor=actor,
            entity_type=AuditEntityType.SCIM_TOKEN,
            entity_id=str(token_id),
            action=AuditAction.REVOKE,
            changes={"after": {"revoked_at": row.revoked_at.isoformat()}},
        )

        return ScimToken(
            id=row.id,
            name=row.name,
            description=row.description,
            token_hint=row.token[-4:],
            created_by=row.created_by,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            created_at=row.created_at,
        )

    async def validate_token(self, db: AsyncSession, token_value: str) -> bool:
        """Validate a SCIM bearer token.

        Checks that the token exists, is not revoked, and is not expired.
        Updates last_used_at on success.

        Returns True if valid, False otherwise.
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                UPDATE scim_tokens
                SET last_used_at = :now
                WHERE token = :token
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > :now)
                RETURNING id
            """),
            {"token": token_value, "now": now},
        )
        return result.fetchone() is not None
