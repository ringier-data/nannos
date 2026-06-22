"""Unit tests for BudgetGuard (console-backend poll + OIDC service token)."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.budget_guard import (
    BudgetGuard,
    BudgetStatus,
    get_budget_guard,
    init_budget_guard,
)


def _oauth(token: str = "svc-token") -> Mock:
    """A stand-in OidcOAuth2Client whose get_token() returns a fixed service token."""
    client = Mock()
    client.get_token = AsyncMock(return_value=token)
    return client


def _http_response(status_code: int = 200, payload: dict | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json = Mock(return_value=payload or {})
    return resp


def _guard_with_http(payload: dict | None = None, *, status_code: int = 200, **kw) -> BudgetGuard:
    """An operational guard whose HTTP client returns a canned status response."""
    guard = BudgetGuard(base_url="http://console", oauth2_client=_oauth(), **kw)
    http = Mock()
    http.is_closed = False
    http.get = AsyncMock(return_value=_http_response(status_code, payload))
    guard._client = http
    return guard


def _status_payload(spend="50.00", limit="100.00", pct=50.0, is_locked=False, warnings=None) -> dict:
    return {
        "enabled": True,
        "spend_usd": spend,
        "limit_usd": limit,
        "usage_percentage": pct,
        "is_locked": is_locked,
        "warnings": warnings or [],
        "period_start": "2026-06-01T00:00:00+00:00",
        "period_end": "2026-06-20T00:00:00+00:00",
    }


class TestBudgetStatus:
    def test_creation(self):
        status = BudgetStatus(
            spend_usd=Decimal("50"),
            limit_usd=Decimal("100"),
            usage_percentage=50.0,
            is_locked=False,
            last_refresh=datetime.now(timezone.utc),
            warnings=[0.8],
        )
        assert status.spend_usd == Decimal("50")
        assert status.usage_percentage == 50.0
        assert status.warnings == [0.8]


class TestInitialization:
    def test_inert_guard_never_locks(self):
        """No OIDC client (local dev) → inert: never enforces, even if internally locked."""
        guard = BudgetGuard(base_url="http://console", oauth2_client=None)
        guard._is_locked = True
        assert guard.enabled is False
        assert guard.is_locked is False

    def test_operational_with_oauth_client(self):
        guard = BudgetGuard(base_url="http://console/", oauth2_client=_oauth())
        assert guard.enabled is True
        assert guard.base_url == "http://console"  # trailing slash stripped


class TestLocking:
    def test_lock_unlock(self):
        guard = BudgetGuard(base_url="http://c", oauth2_client=_oauth())
        guard._lock("boom")
        assert guard.is_locked is True
        assert guard._lock_reason == "boom"
        guard._unlock()
        assert guard.is_locked is False
        assert guard._lock_reason is None


class TestRefresh:
    @pytest.mark.asyncio
    async def test_inert_noop(self):
        guard = BudgetGuard(base_url="http://c", oauth2_client=None)
        await guard.refresh()
        assert guard.is_locked is False

    @pytest.mark.asyncio
    async def test_success_under_budget_unlocked(self):
        guard = _guard_with_http(_status_payload(spend="50.00", limit="100.00", pct=50.0))
        await guard.refresh()
        assert guard.is_locked is False
        assert guard._spend_usd == Decimal("50.00")
        assert guard._limit_usd == Decimal("100.00")
        assert guard._usage_percentage == 50.0
        assert guard._last_refresh is not None

    @pytest.mark.asyncio
    async def test_success_locked_when_server_says_locked(self):
        guard = _guard_with_http(
            _status_payload(spend="120.00", limit="100.00", pct=120.0, is_locked=True)
        )
        await guard.refresh()
        assert guard.is_locked is True

    @pytest.mark.asyncio
    async def test_warnings_passed_through(self):
        guard = _guard_with_http(_status_payload(pct=85.0, warnings=[0.8]))
        await guard.refresh()
        assert guard.get_status().warnings == [0.8]
        assert guard.is_locked is False

    @pytest.mark.asyncio
    async def test_recovers_unlock_after_lock(self):
        guard = _guard_with_http(_status_payload(is_locked=True))
        await guard.refresh()
        assert guard.is_locked is True
        # Next poll comes back healthy → unlock.
        guard._client.get = AsyncMock(return_value=_http_response(200, _status_payload(is_locked=False)))
        await guard.refresh()
        assert guard.is_locked is False

    @pytest.mark.asyncio
    async def test_fail_closed_on_token_error(self):
        guard = BudgetGuard(base_url="http://c", oauth2_client=_oauth())
        guard.oauth2_client.get_token = AsyncMock(side_effect=Exception("no idp"))
        await guard.refresh()
        assert guard.is_locked is True
        assert "service token" in guard._lock_reason

    @pytest.mark.asyncio
    async def test_fail_closed_on_non_200(self):
        guard = _guard_with_http(status_code=500)
        await guard.refresh()
        assert guard.is_locked is True
        assert "500" in guard._lock_reason

    @pytest.mark.asyncio
    async def test_fail_closed_on_network_error(self):
        guard = BudgetGuard(base_url="http://c", oauth2_client=_oauth())
        http = Mock()
        http.is_closed = False
        http.get = AsyncMock(side_effect=Exception("connection refused"))
        guard._client = http
        await guard.refresh()
        assert guard.is_locked is True
        assert "poll failed" in guard._lock_reason


class TestPolling:
    @pytest.mark.asyncio
    async def test_start_polling_inert(self):
        guard = BudgetGuard(base_url="http://c", oauth2_client=None)
        await guard.start_polling()
        assert guard._polling_task is None

    @pytest.mark.asyncio
    async def test_start_and_stop_polling(self):
        guard = BudgetGuard(base_url="http://c", oauth2_client=_oauth(), check_interval_seconds=1)
        guard.refresh = AsyncMock()
        await guard.start_polling()
        assert guard._polling_task is not None
        await asyncio.sleep(0.05)
        guard.refresh.assert_called()
        await guard.stop_polling()
        assert guard._polling_task is None

    @pytest.mark.asyncio
    async def test_start_polling_idempotent(self):
        guard = BudgetGuard(base_url="http://c", oauth2_client=_oauth(), check_interval_seconds=1)
        guard.refresh = AsyncMock()
        await guard.start_polling()
        task1 = guard._polling_task
        await guard.start_polling()
        assert guard._polling_task is task1
        await guard.stop_polling()


class TestSingleton:
    def test_init_and_get(self):
        import app.core.budget_guard as bg_module

        bg_module._budget_guard = None
        guard = init_budget_guard(base_url="http://console", oauth2_client=_oauth())
        assert get_budget_guard() is guard
        assert guard.enabled is True

    def test_get_returns_none_before_init(self):
        import app.core.budget_guard as bg_module

        bg_module._budget_guard = None
        assert get_budget_guard() is None
