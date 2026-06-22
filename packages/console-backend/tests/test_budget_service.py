"""Unit tests for BudgetService.get_status lock math (no DB; repo + usage mocked)."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from console_backend.models.usage import UsageSummary
from console_backend.services.budget_service import BudgetService


def _service(*, enabled=True, limit="100.00", thresholds=None, spend="0.00") -> BudgetService:
    svc = BudgetService()

    repo = Mock()
    repo.get = AsyncMock(
        return_value={
            "enabled": enabled,
            "monthly_limit_usd": Decimal(limit),
            "warning_thresholds": thresholds if thresholds is not None else [0.8, 0.9, 0.95],
            "updated_at": datetime.now(timezone.utc),
        }
    )
    svc.set_repository(repo)

    usage = Mock()
    usage.get_global_summary = AsyncMock(
        return_value=UsageSummary(
            total_cost_usd=Decimal(spend),
            total_requests=1,
            period_start=datetime.now(timezone.utc),
            period_end=datetime.now(timezone.utc),
        )
    )
    svc.set_usage_service(usage)
    return svc


@pytest.mark.asyncio
async def test_under_budget_not_locked():
    status = await _service(spend="50.00", limit="100.00").get_status(db=Mock())
    assert status.is_locked is False
    assert status.usage_percentage == 50.0
    assert status.warnings == []


@pytest.mark.asyncio
async def test_at_limit_locks():
    status = await _service(spend="100.00", limit="100.00").get_status(db=Mock())
    assert status.is_locked is True
    assert status.usage_percentage == 100.0


@pytest.mark.asyncio
async def test_over_limit_locks():
    status = await _service(spend="150.00", limit="100.00").get_status(db=Mock())
    assert status.is_locked is True
    assert status.usage_percentage == 150.0


@pytest.mark.asyncio
async def test_disabled_never_locks_even_over_budget():
    status = await _service(enabled=False, spend="200.00", limit="100.00").get_status(db=Mock())
    assert status.is_locked is False
    assert status.enabled is False


@pytest.mark.asyncio
async def test_warnings_reported_at_crossed_thresholds():
    status = await _service(spend="92.00", limit="100.00", thresholds=[0.8, 0.9, 0.95]).get_status(
        db=Mock()
    )
    assert status.is_locked is False
    assert status.warnings == [0.8, 0.9]  # 0.92 crosses 80% and 90%, not 95%


@pytest.mark.asyncio
async def test_zero_limit_does_not_divide_by_zero_or_lock():
    status = await _service(spend="10.00", limit="0.00").get_status(db=Mock())
    assert status.usage_percentage == 0.0
    assert status.is_locked is False
