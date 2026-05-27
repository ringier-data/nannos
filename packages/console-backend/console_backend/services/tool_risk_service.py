"""Service for tool risk scores CRUD operations.

Write operations (upsert/delete) are routed through ToolRiskRepository
to ensure automatic audit logging. Read operations use direct SQL for
performance (no audit needed).
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user import User
from ..repositories.tool_risk_repository import ToolRiskRepository

logger = logging.getLogger(__name__)


class ToolRiskService:
    """Service for tool_risk_scores table, backed by ToolRiskRepository."""

    def __init__(self) -> None:
        self._repo: ToolRiskRepository | None = None

    def set_repository(self, repo: ToolRiskRepository) -> None:
        """Inject repository dependency."""
        self._repo = repo

    @property
    def repo(self) -> ToolRiskRepository:
        if self._repo is None:
            raise RuntimeError(
                "ToolRiskRepository not injected into ToolRiskService. Call set_repository() during initialization."
            )
        return self._repo

    async def get_score(
        self,
        db: AsyncSession,
        tool_name: str,
        server_slug: str,
    ) -> dict[str, Any] | None:
        """Get a single risk score by tool_name and server_slug."""
        return await self.repo.get_score(db, tool_name, server_slug)

    async def get_scores_paginated(
        self,
        db: AsyncSession,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get paginated scores sorted by updated_at desc (most recent first)."""
        return await self.repo.get_scores_paginated(db, limit=limit, offset=offset)

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
        """Upsert a risk score entry (audited)."""
        return await self.repo.upsert_score(
            db,
            actor=actor,
            tool_name=tool_name,
            server_slug=server_slug,
            schema_hash=schema_hash,
            base_score=base_score,
            risk_factors=risk_factors,
            allowed_actions=allowed_actions,
        )

    async def delete_score(
        self,
        db: AsyncSession,
        actor: User,
        tool_name: str,
        server_slug: str,
    ) -> bool:
        """Delete a risk score (audited). Returns True if deleted."""
        return await self.repo.delete_score(db, actor=actor, tool_name=tool_name, server_slug=server_slug)

    async def get_count(self, db: AsyncSession) -> int:
        """Get total count of risk scores."""
        return await self.repo.get_count(db)
