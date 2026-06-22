"""Repository for per-role default model aliases with audit logging.

`model_defaults` is keyed on `role` (one alias per role) and written as an upsert, so we
override the base-class CRUD (which assumes a single `id` column) with a role-keyed
upsert that records the change automatically — keeping the SET_DEFAULT audit trail on the
repository write itself (AGENTS.md: all writes go through the repository pattern), not as a
hand-rolled call at the router.
"""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.model_gateway import CHAT_TIER_ROLES
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


class ModelDefaultsRepository(AuditedRepository):
    """Repository for model_defaults (role → alias) with automatic audit trail."""

    def __init__(self):
        super().__init__(
            entity_type=AuditEntityType.MODEL_DEFAULT,
            table_name="model_defaults",
        )

    async def get_all(self, db: AsyncSession) -> dict[str, str]:
        """{role: model_alias} for every role that has a default set."""
        result = await db.execute(text("SELECT role, model_alias FROM model_defaults"))
        return {row.role: row.model_alias for row in result}

    async def get_alias_tiers(self, db: AsyncSession) -> dict[str, list[str]]:
        """{alias: [chat-tier roles]} — every chat tier each alias has served as default.

        Used to degrade a retired concrete-model sub-agent to its tier's successor instead of
        the standard chat default (model_alias_tiers, migration 069). An alias can serve several
        tiers at once, so each maps to a list."""
        result = await db.execute(text("SELECT alias, role FROM model_alias_tiers"))
        tiers: dict[str, list[str]] = {}
        for row in result:
            tiers.setdefault(row.alias, []).append(row.role)
        return tiers

    async def upsert_default(
        self,
        db: AsyncSession,
        actor: User,
        role: str,
        model_alias: str,
    ) -> None:
        """Upsert the default alias for a role (exactly one alias per role), with audit."""
        before = (await self.get_all(db)).get(role)

        await db.execute(
            text(
                """
                INSERT INTO model_defaults (role, model_alias, updated_at)
                VALUES (:role, :alias, NOW())
                ON CONFLICT (role) DO UPDATE
                    SET model_alias = EXCLUDED.model_alias, updated_at = NOW()
                """
            ),
            {"role": role, "alias": model_alias},
        )

        # Remember which chat tier this alias served, so a retired concrete-model sub-agent
        # can later degrade to the tier's successor (migration 069). Most-recent role wins.
        if role in CHAT_TIER_ROLES:
            await db.execute(
                text(
                    """
                    INSERT INTO model_alias_tiers (alias, role, updated_at)
                    VALUES (:alias, :role, NOW())
                    ON CONFLICT (alias, role) DO UPDATE
                        SET updated_at = NOW()
                    """
                ),
                {"alias": model_alias, "role": role},
            )

        changes: dict[str, Any] = {"before": before, "after": model_alias}
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=role,
            action=AuditAction.SET_DEFAULT,
            changes=changes,
        )

        await db.commit()
        logger.info("Set default for role=%s to '%s' by %s", role, model_alias, actor.sub)
