"""Per-role default model aliases (graceful degradation, ADR-0001).

Authoritative store for the fleet default chat / embedding / multimodal-embedding
model. Lives here (not in the gateway model_info) because LiteLLM's /model/update
can't persist a custom default flag — only /model/new can — so a DB-backed,
runtime-editable record is the robust home. The apps read these via the unauthenticated
in-cluster /api/v1/models/defaults endpoint and fall back to them when a referenced
alias has been retired.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

VALID_ROLES = ("chat", "embedding", "multimodal_embedding")


class ModelDefaultsService:
    async def get_all(self, db: AsyncSession) -> dict[str, str]:
        """{role: model_alias} for every role that has a default set."""
        result = await db.execute(text("SELECT role, model_alias FROM model_defaults"))
        return {row.role: row.model_alias for row in result}

    async def set_default(self, db: AsyncSession, role: str, model_alias: str) -> None:
        """Upsert the default alias for a role (exactly one alias per role)."""
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
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
