"""Repository for tool risk scores with audit logging.

Uses a composite primary key (tool_name, server_slug) so we override
the base class CRUD methods that assume a single `id` column.
"""

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User
from .base import AuditedRepository, _serialize_for_audit

logger = logging.getLogger(__name__)


class ToolRiskRepository(AuditedRepository):
    """Repository for tool_risk_scores with automatic audit trail."""

    def __init__(self):
        super().__init__(
            entity_type=AuditEntityType.TOOL_RISK_SCORE,
            table_name="tool_risk_scores",
        )

    async def get_score(
        self,
        db: AsyncSession,
        tool_name: str,
        server_slug: str,
    ) -> dict[str, Any] | None:
        """Get a single risk score by composite key."""
        result = await db.execute(
            text("""
                SELECT tool_name, server_slug, schema_hash, base_score,
                       risk_factors, allowed_actions, updated_at, created_at
                FROM tool_risk_scores
                WHERE tool_name = :tool_name AND server_slug = :server_slug
            """),
            {"tool_name": tool_name, "server_slug": server_slug},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_scores_paginated(
        self,
        db: AsyncSession,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get paginated scores sorted by updated_at desc."""
        result = await db.execute(
            text("""
                SELECT tool_name, server_slug, schema_hash, base_score,
                       risk_factors, allowed_actions, updated_at, created_at
                FROM tool_risk_scores
                ORDER BY updated_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {"limit": limit, "offset": offset},
        )
        return [dict(row) for row in result.mappings().all()]

    async def get_count(self, db: AsyncSession) -> int:
        """Get total count of risk scores."""
        result = await db.execute(text("SELECT COUNT(*) FROM tool_risk_scores"))
        return result.scalar() or 0

    async def upsert_score(
        self,
        db: AsyncSession,
        actor: User,
        tool_name: str,
        server_slug: str,
        schema_hash: str,
        base_score: float,
        risk_factors: dict[str, Any],
        allowed_actions: list[str],
    ) -> dict[str, Any]:
        """Upsert a risk score with audit logging."""
        # Check if record exists (for audit before/after)
        before = await self.get_score(db, tool_name, server_slug)

        result = await db.execute(
            text("""
                INSERT INTO tool_risk_scores
                    (tool_name, server_slug, schema_hash, base_score, risk_factors, allowed_actions, updated_at)
                VALUES
                    (:tool_name, :server_slug, :schema_hash, :base_score,
                     CAST(:risk_factors AS jsonb), CAST(:allowed_actions AS jsonb), NOW())
                ON CONFLICT (tool_name, server_slug)
                DO UPDATE SET
                    schema_hash = EXCLUDED.schema_hash,
                    base_score = EXCLUDED.base_score,
                    risk_factors = EXCLUDED.risk_factors,
                    allowed_actions = EXCLUDED.allowed_actions,
                    updated_at = NOW()
                RETURNING tool_name, server_slug, schema_hash, base_score,
                          risk_factors, allowed_actions, updated_at, created_at
            """),
            {
                "tool_name": tool_name,
                "server_slug": server_slug,
                "schema_hash": schema_hash,
                "base_score": base_score,
                "risk_factors": json.dumps(risk_factors),
                "allowed_actions": json.dumps(allowed_actions),
            },
        )
        row = result.mappings().first()
        entity_id = f"{tool_name}:{server_slug}"

        action = AuditAction.UPDATE if before else AuditAction.CREATE
        changes: dict[str, Any] = {
            "after": _serialize_for_audit(
                {
                    "tool_name": tool_name,
                    "server_slug": server_slug,
                    "schema_hash": schema_hash,
                    "base_score": base_score,
                    "risk_factors": risk_factors,
                    "allowed_actions": allowed_actions,
                }
            )
        }
        if before:
            changes["before"] = _serialize_for_audit(
                {
                    "schema_hash": before["schema_hash"],
                    "base_score": before["base_score"],
                    "risk_factors": before["risk_factors"],
                    "allowed_actions": before["allowed_actions"],
                }
            )

        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=entity_id,
            action=action,
            changes=changes,
        )

        await db.commit()
        return dict(row) if row else {}

    async def delete_score(
        self,
        db: AsyncSession,
        actor: User,
        tool_name: str,
        server_slug: str,
    ) -> bool:
        """Delete a risk score with audit logging. Returns True if deleted."""
        entity_id = f"{tool_name}:{server_slug}"

        result = await db.execute(
            text("""
                DELETE FROM tool_risk_scores
                WHERE tool_name = :tool_name AND server_slug = :server_slug
            """),
            {"tool_name": tool_name, "server_slug": server_slug},
        )

        deleted = result.rowcount > 0
        if deleted:
            await self.audit_service.log_action(
                db=db,
                actor=actor,
                entity_type=self.entity_type,
                entity_id=entity_id,
                action=AuditAction.DELETE,
                changes={"tool_name": tool_name, "server_slug": server_slug},
            )

        await db.commit()
        return deleted
