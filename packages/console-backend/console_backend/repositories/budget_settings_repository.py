"""Repository for the single-row Budget Guard configuration, with audit logging.

`budget_settings` holds exactly one row (`id = TRUE`), so the base-class CRUD (which
assumes an arbitrary `id` and a soft-delete column) doesn't fit. We provide a `get` and a
partial `update_settings` that upserts the singleton and records the change automatically
(AGENTS.md: all writes go through the repository so the audit trail is on the write itself,
not hand-rolled at the router).
"""

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)

# Columns an admin may set; `warning_thresholds` is bound as JSON separately.
_UPDATABLE = ("enabled", "monthly_limit_usd", "warning_thresholds")


class BudgetSettingsRepository(AuditedRepository):
    """Repository for the singleton budget_settings row with an automatic audit trail."""

    def __init__(self):
        super().__init__(
            entity_type=AuditEntityType.BUDGET_SETTING,
            table_name="budget_settings",
        )

    async def get(self, db: AsyncSession) -> dict[str, Any]:
        """Return the single budget_settings row as a dict.

        The row is seeded by migration 066, so this always returns one row; the COALESCE
        guards a (theoretically impossible) empty table by inserting defaults on read.
        """
        result = await db.execute(
            text(
                """
                SELECT enabled, monthly_limit_usd, warning_thresholds, updated_at
                FROM budget_settings
                WHERE id IS TRUE
                """
            )
        )
        row = result.mappings().first()
        if row is None:  # defensive: re-seed if the singleton went missing
            await db.execute(text("INSERT INTO budget_settings (id) VALUES (TRUE) ON CONFLICT (id) DO NOTHING"))
            await db.commit()
            return await self.get(db)
        return dict(row)

    async def update_settings(
        self,
        db: AsyncSession,
        actor: User,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply a partial update to the singleton row and audit the before/after.

        `fields` may contain any subset of {enabled, monthly_limit_usd, warning_thresholds}.
        `warning_thresholds` (a list) is serialized to JSON for the JSONB column.
        """
        fields = {k: v for k, v in fields.items() if k in _UPDATABLE and v is not None}
        if not fields:
            return await self.get(db)

        before = await self.get(db)

        set_clause = ", ".join(f"{k} = :{k}" for k in fields)
        params: dict[str, Any] = dict(fields)
        if "warning_thresholds" in params:
            # JSONB column: bind as a JSON string and cast in SQL.
            params["warning_thresholds"] = json.dumps(params["warning_thresholds"])
            set_clause = set_clause.replace(
                "warning_thresholds = :warning_thresholds",
                "warning_thresholds = CAST(:warning_thresholds AS JSONB)",
            )

        await db.execute(
            text(f"UPDATE budget_settings SET {set_clause}, updated_at = NOW() WHERE id IS TRUE"),
            params,
        )

        after = await self.get(db)
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id="global",
            action=AuditAction.UPDATE,
            changes={
                "before": {k: str(before.get(k)) for k in fields},
                "after": {k: str(after.get(k)) for k in fields},
            },
        )

        await db.commit()
        logger.info("Budget settings updated (%s) by %s", ", ".join(fields), actor.sub)
        return after
