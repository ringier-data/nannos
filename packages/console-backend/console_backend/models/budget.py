"""Pydantic models for the Budget Guard (monthly USD spend cap).

The Budget Guard caps global LLM spend over a calendar month. Its configuration lives
in the single-row `budget_settings` table (admin-editable from the console); the live
spend/lock decision is computed from `usage_logs.total_cost_usd`. The orchestrator polls
`BudgetStatus` and locks LLM traffic when `is_locked` is true (fail-closed).
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


def _validate_thresholds(v: list[float]) -> list[float]:
    """Thresholds are fractions of the limit in (0, 1], returned sorted ascending."""
    for t in v:
        if not (0 < t <= 1):
            raise ValueError(f"warning threshold {t} must be in the range (0, 1]")
    return sorted(set(v))


class BudgetSettings(BaseModel):
    """Current Budget Guard configuration (the single `budget_settings` row)."""

    enabled: bool
    monthly_limit_usd: Decimal
    warning_thresholds: list[float]
    updated_at: datetime


class BudgetSettingsUpdate(BaseModel):
    """Partial update to the Budget Guard configuration (admin-only).

    Every field is optional so the admin form can PATCH-style update a subset.
    """

    enabled: bool | None = None
    monthly_limit_usd: Decimal | None = Field(None, gt=0, description="Monthly spend ceiling in USD")
    warning_thresholds: list[float] | None = Field(
        None, description="Fractions of the limit (0..1] at which to warn, e.g. [0.8, 0.9, 0.95]"
    )

    @field_validator("warning_thresholds")
    @classmethod
    def _check_thresholds(cls, v: list[float] | None) -> list[float] | None:
        return _validate_thresholds(v) if v is not None else v


class BudgetStatus(BaseModel):
    """Live budget snapshot: month-to-date spend vs limit, plus the lock decision.

    Served to both the admin page (to render the gauge) and the orchestrator poll (to
    drive enforcement). `is_locked` is the single source of truth for enforcement.
    """

    enabled: bool
    spend_usd: Decimal = Field(..., description="Month-to-date global spend in USD")
    limit_usd: Decimal
    usage_percentage: float = Field(..., description="spend / limit as a percentage")
    is_locked: bool = Field(..., description="True when enabled and spend >= limit (fail-closed)")
    warnings: list[float] = Field(
        default_factory=list, description="Warning thresholds crossed at the current spend"
    )
    period_start: datetime = Field(..., description="Start of the current calendar month (UTC)")
    period_end: datetime = Field(..., description="When this snapshot was computed (UTC)")
