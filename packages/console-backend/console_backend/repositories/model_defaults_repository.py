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

    # --- Tier groups (nannos#204) --------------------------------------------------------
    # A tier group is the ordered list of aliases serving one chat tier: its head is the
    # tier's default (model_defaults, above) and its tail is the failover chain stored here.
    # Only the tail is persisted, so re-pointing a tier's default re-heads its chain for
    # free rather than leaving two rows to disagree about which alias comes first.

    async def get_all_fallbacks(self, db: AsyncSession) -> dict[str, list[str]]:
        """{role: [alias, ...]} in chain order, for every role with a failover chain."""
        result = await db.execute(text("SELECT role, alias FROM model_tier_fallbacks ORDER BY role, position"))
        chains: dict[str, list[str]] = {}
        for row in result:
            chains.setdefault(row.role, []).append(row.alias)
        return chains

    async def get_fallbacks(self, db: AsyncSession, role: str) -> list[str]:
        """The failover chain for one role, in order (empty when none is configured)."""
        result = await db.execute(
            text("SELECT alias FROM model_tier_fallbacks WHERE role = :role ORDER BY position"),
            {"role": role},
        )
        return [row.alias for row in result]

    async def replace_fallbacks(
        self,
        db: AsyncSession,
        actor: User,
        role: str,
        aliases: list[str],
    ) -> None:
        """Replace a role's failover chain wholesale, with audit.

        Replace rather than patch: the chain is an ordered whole and an admin edits it as
        one, so a partial write has no meaning. The (role, position) unique constraint is
        DEFERRABLE, so delete-then-insert inside one transaction never trips it on a
        reorder that reuses positions.
        """
        before = await self.get_fallbacks(db, role)

        await db.execute(text("DELETE FROM model_tier_fallbacks WHERE role = :role"), {"role": role})
        for position, alias in enumerate(aliases, start=1):
            await db.execute(
                text(
                    """
                    INSERT INTO model_tier_fallbacks (role, alias, position, updated_at)
                    VALUES (:role, :alias, :position, NOW())
                    """
                ),
                {"role": role, "alias": alias, "position": position},
            )

        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=role,
            action=AuditAction.SET_DEFAULT,
            changes={"before": before, "after": aliases, "field": "fallbacks"},
        )

        await db.commit()
        logger.info("Set failover chain for role=%s to %s by %s", role, aliases, actor.sub)
