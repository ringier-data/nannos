"""Per-role default model aliases (graceful degradation).

Authoritative store for the fleet default chat / embedding / multimodal-embedding
model. Lives here (not in the gateway model_info) because LiteLLM's /model/update
can't persist a custom default flag — only /model/new can — so a DB-backed,
runtime-editable record is the robust home. The apps read these via the unauthenticated
in-cluster /api/v1/models/defaults endpoint and fall back to them when a referenced
alias has been retired.
"""

import logging
from typing import TYPE_CHECKING, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.model_gateway import VALID_ROLES  # single source of truth for role keys
from ..models.user import User

if TYPE_CHECKING:
    from ..repositories.model_defaults_repository import ModelDefaultsRepository

logger = logging.getLogger(__name__)


class ModelDefaultsService:
    def __init__(self) -> None:
        self._repository: Optional["ModelDefaultsRepository"] = None

    def set_repository(self, repository: "ModelDefaultsRepository") -> None:
        """Inject the repository (writes go through it so audit logging is automatic)."""
        self._repository = repository

    @property
    def repository(self) -> "ModelDefaultsRepository":
        if self._repository is None:
            raise RuntimeError("ModelDefaultsRepository not injected. Call set_repository() during init.")
        return self._repository

    async def get_all(self, db: AsyncSession) -> dict[str, str]:
        """{role: model_alias} for every role that has a default set."""
        return await self.repository.get_all(db)

    async def get_alias_tiers(self, db: AsyncSession) -> dict[str, str]:
        """{alias: chat-tier role} — the most-recent chat tier each alias served as default."""
        return await self.repository.get_alias_tiers(db)

    async def set_default(self, db: AsyncSession, actor: User, role: str, model_alias: str) -> None:
        """Upsert the default alias for a role (exactly one alias per role).

        Writes through the audited repository so the fleet-wide config change is recorded
        automatically (AGENTS.md repository-pattern rule)."""
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        await self.repository.upsert_default(db, actor=actor, role=role, model_alias=model_alias)
