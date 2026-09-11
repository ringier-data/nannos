"""Repository for embed bindings (ADR-0006): which host authority a sub-agent is bound to,
and which token ``azp`` values select it."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


class EmbedBindingRepository(AuditedRepository):
    """Audited writes to ``sub_agent_embed_bindings`` and its ``_azps`` side table.

    A binding is a property of a sub-agent, so every change is audited as an UPDATE of
    the sub-agent (entity ``sub_agent``, id = ``sub_agent_id``) carrying the binding
    before and after: the trail lands on the sub-agent's audit page and needs no new
    entity type. Only admin decisions go through here — base URL and azp list. Sync
    bookkeeping (revision, definition, fetched_at, last_error, last_seen_at, azps_seen)
    records what the host published or when a token was seen, not a decision; the
    service writes it directly (the cache-bookkeeping exemption in AGENTS.md).
    """

    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.SUB_AGENT,
            table_name="sub_agent_embed_bindings",
        )

    async def snapshot(self, db: AsyncSession, sub_agent_id: int) -> dict[str, Any] | None:
        """The admin-owned part of the binding — base URL and azps — or None when unbound."""
        result = await db.execute(
            text(
                """
                SELECT b.base_url,
                       COALESCE(array_agg(a.azp ORDER BY a.azp) FILTER (WHERE a.azp IS NOT NULL), '{}')
                  FROM sub_agent_embed_bindings b
                  LEFT JOIN sub_agent_embed_binding_azps a ON a.sub_agent_id = b.sub_agent_id
                 WHERE b.sub_agent_id = :id
                 GROUP BY b.sub_agent_id
                """
            ),
            {"id": sub_agent_id},
        )
        row = result.first()
        if row is None:
            return None
        return {"base_url": row[0], "azps": list(row[1] or [])}

    async def write_binding(
        self,
        db: AsyncSession,
        actor: User,
        sub_agent_id: int,
        *,
        base_url: str,
        azps: list[str],
    ) -> None:
        """Insert or replace the binding row and its azp rows. No commit."""
        before = await self.snapshot(db, sub_agent_id)
        now = datetime.now(timezone.utc)
        await db.execute(
            text(
                """
                INSERT INTO sub_agent_embed_bindings (sub_agent_id, base_url, created_by, created_at, updated_at)
                VALUES (:id, :base_url, :created_by, :now, :now)
                ON CONFLICT (sub_agent_id) DO UPDATE
                   SET base_url = EXCLUDED.base_url,
                       -- A new host means a new definition: forget the old revision so the
                       -- sync writes a version even if the new host's revision collides, and
                       -- drop the old definition so the admin view never shows the previous
                       -- host's agent and skills under the new URL while its first sync fails.
                       revision = CASE WHEN sub_agent_embed_bindings.base_url = EXCLUDED.base_url
                                       THEN sub_agent_embed_bindings.revision ELSE NULL END,
                       definition = CASE WHEN sub_agent_embed_bindings.base_url = EXCLUDED.base_url
                                         THEN sub_agent_embed_bindings.definition ELSE NULL END,
                       updated_at = EXCLUDED.updated_at
                """
            ),
            {"id": sub_agent_id, "base_url": base_url, "created_by": actor.id, "now": now},
        )
        await db.execute(
            text("DELETE FROM sub_agent_embed_binding_azps WHERE sub_agent_id = :id"),
            {"id": sub_agent_id},
        )
        for azp in azps:
            await db.execute(
                text(
                    "INSERT INTO sub_agent_embed_binding_azps (azp, sub_agent_id) VALUES (:azp, :id)"
                ),
                {"azp": azp, "id": sub_agent_id},
            )
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=str(sub_agent_id),
            action=AuditAction.UPDATE,
            changes={
                "before": {"embed_binding": before},
                "after": {"embed_binding": {"base_url": base_url, "azps": list(azps)}},
            },
        )

    async def delete_binding(self, db: AsyncSession, actor: User, sub_agent_id: int) -> bool:
        """Remove the binding (its azp rows cascade). False when there was none. No commit."""
        before = await self.snapshot(db, sub_agent_id)
        if before is None:
            return False
        await db.execute(
            text("DELETE FROM sub_agent_embed_bindings WHERE sub_agent_id = :id"),
            {"id": sub_agent_id},
        )
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=str(sub_agent_id),
            action=AuditAction.UPDATE,
            changes={"before": {"embed_binding": before}, "after": {"embed_binding": None}},
        )
        return True
