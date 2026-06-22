"""Budget Guard service: admin-editable config + the live spend/lock decision.

This is the single authority for the Budget Guard. It reads the singleton config from
`budget_settings` and computes month-to-date global spend from `usage_logs` (via
`UsageService.get_global_summary`). Both the admin page and the orchestrator's enforcement
poll consume `get_status()`, so the lock math lives in exactly one place.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.budget import BudgetSettings, BudgetSettingsUpdate, BudgetStatus
from ..models.user import User

if TYPE_CHECKING:
    from ..repositories.budget_settings_repository import BudgetSettingsRepository
    from .usage_service import UsageService

logger = logging.getLogger(__name__)


def _month_start(now: datetime) -> datetime:
    """Start of the current calendar month in UTC."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BudgetService:
    def __init__(self) -> None:
        self._repository: Optional["BudgetSettingsRepository"] = None
        self._usage_service: Optional["UsageService"] = None

    def set_repository(self, repository: "BudgetSettingsRepository") -> None:
        """Inject the repository (writes go through it so audit logging is automatic)."""
        self._repository = repository

    def set_usage_service(self, usage_service: "UsageService") -> None:
        """Inject the usage service (source of month-to-date spend)."""
        self._usage_service = usage_service

    @property
    def repository(self) -> "BudgetSettingsRepository":
        if self._repository is None:
            raise RuntimeError("BudgetSettingsRepository not injected. Call set_repository() during init.")
        return self._repository

    @property
    def usage_service(self) -> "UsageService":
        if self._usage_service is None:
            raise RuntimeError("UsageService not injected. Call set_usage_service() during init.")
        return self._usage_service

    async def get_settings(self, db: AsyncSession) -> BudgetSettings:
        """Return the current Budget Guard configuration."""
        row = await self.repository.get(db)
        return BudgetSettings(**row)

    async def update_settings(
        self, db: AsyncSession, actor: User, update: BudgetSettingsUpdate
    ) -> BudgetSettings:
        """Apply a partial, audited update to the configuration."""
        row = await self.repository.update_settings(
            db, actor=actor, fields=update.model_dump(exclude_none=True)
        )
        return BudgetSettings(**row)

    async def get_status(self, db: AsyncSession) -> BudgetStatus:
        """Compute the live budget snapshot: month-to-date spend vs limit + lock decision.

        `is_locked` is true only when the guard is enabled AND spend has reached the limit;
        this is the single source of truth the orchestrator enforces against.
        """
        settings = await self.get_settings(db)
        now = datetime.now(timezone.utc)
        period_start = _month_start(now)

        summary = await self.usage_service.get_global_summary(db, start_date=period_start, end_date=now)
        spend = Decimal(summary.total_cost_usd or 0)
        limit = settings.monthly_limit_usd

        ratio = float(spend / limit) if limit > 0 else 0.0
        is_locked = settings.enabled and limit > 0 and spend >= limit
        warnings = [t for t in settings.warning_thresholds if ratio >= t]

        return BudgetStatus(
            enabled=settings.enabled,
            spend_usd=spend,
            limit_usd=limit,
            usage_percentage=round(ratio * 100, 2),
            is_locked=is_locked,
            warnings=warnings,
            period_start=period_start,
            period_end=now,
        )
