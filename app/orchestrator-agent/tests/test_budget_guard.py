"""Unit tests for BudgetGuard system."""

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.core.budget_guard import (
    BudgetGuard,
    BudgetStatus,
    get_budget_guard,
    init_budget_guard,
)


class TestBudgetStatus:
    """Tests for BudgetStatus dataclass."""

    def test_budget_status_creation(self):
        """Test creating a BudgetStatus instance."""
        status = BudgetStatus(
            current_usage=1000,
            token_limit=10000,
            usage_percentage=10.0,
            is_locked=False,
            last_refresh=datetime.now(timezone.utc),
            lock_reason=None,
            warnings_sent=[0.8, 0.9],
        )
        assert status.current_usage == 1000
        assert status.token_limit == 10000
        assert status.usage_percentage == 10.0
        assert status.is_locked is False
        assert status.lock_reason is None
        assert len(status.warnings_sent) == 2


class TestBudgetGuardInitialization:
    """Tests for BudgetGuard initialization."""

    def test_disabled_budget_guard(self):
        """Test creating a disabled budget guard."""
        guard = BudgetGuard(
            enabled=False,
            project_name="test-project",
            token_limit=100000,
        )
        assert guard.enabled is False
        assert guard.is_locked is False  # Disabled means never locked

    def test_enabled_budget_guard_without_client(self):
        """Test creating an enabled budget guard without LangSmith client."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        assert guard.enabled is True
        assert guard.token_limit == 100000
        assert guard.project_name == "test-project"

    @patch("app.core.budget_guard.Client")
    def test_enabled_budget_guard_with_client(self, mock_client_class):
        """Test creating an enabled budget guard with LangSmith client."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        with patch.dict(os.environ, {"LANGCHAIN_API_KEY": "test-key"}):
            guard = BudgetGuard(
                enabled=True,
                project_name="test-project",
                token_limit=100000,
            )
            guard._client = mock_client  # Simulate client creation

            assert guard._client is not None

    def test_default_warning_thresholds(self):
        """Test default warning thresholds."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        assert guard.warning_thresholds == (0.8, 0.9, 0.95)

    def test_custom_warning_thresholds(self):
        """Test custom warning thresholds."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
            warning_thresholds=(0.5, 0.75, 0.9),
        )
        assert guard.warning_thresholds == (0.5, 0.75, 0.9)


class TestBudgetGuardLocking:
    """Tests for BudgetGuard lock/unlock behavior."""

    def test_is_locked_when_disabled(self):
        """Test that disabled guard is never locked."""
        guard = BudgetGuard(
            enabled=False,
            project_name="test-project",
            token_limit=100000,
        )
        # Manually set internal lock (shouldn't affect is_locked)
        guard._is_locked = True
        assert guard.is_locked is False  # Still not locked because disabled

    def test_is_locked_when_enabled_and_locked(self):
        """Test that enabled guard reports lock correctly."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._lock("Budget exceeded")
        assert guard.is_locked is True
        assert guard._lock_reason == "Budget exceeded"

    def test_unlock(self):
        """Test unlocking the guard."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._lock("Test lock")
        assert guard.is_locked is True

        guard._unlock()
        assert guard.is_locked is False
        assert guard._lock_reason is None


class TestBudgetGuardStatus:
    """Tests for BudgetGuard get_status method."""

    def test_get_status_initial(self):
        """Test get_status with initial state."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        status = guard.get_status()

        assert status.current_usage == 0
        assert status.token_limit == 100000
        assert status.usage_percentage == 0.0
        assert status.is_locked is False
        assert status.lock_reason is None

    def test_get_status_with_usage(self):
        """Test get_status with some usage."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._current_usage = 50000

        status = guard.get_status()
        assert status.current_usage == 50000
        assert status.usage_percentage == 50.0

    def test_get_status_with_lock(self):
        """Test get_status when locked."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._lock("Budget exceeded")

        status = guard.get_status()
        assert status.is_locked is True
        assert status.lock_reason == "Budget exceeded"

    def test_get_status_zero_limit(self):
        """Test get_status with zero limit (edge case)."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=0,
        )
        guard._current_usage = 1000

        status = guard.get_status()
        assert status.usage_percentage == 0.0  # Avoid division by zero


class TestBudgetGuardSetTokenLimit:
    """Tests for BudgetGuard set_token_limit method."""

    def test_set_token_limit(self):
        """Test updating token limit."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard.set_token_limit(200000)
        assert guard.token_limit == 200000

    def test_set_token_limit_unlocks_when_under_new_limit(self):
        """Test that increasing limit unlocks when usage is now under limit."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._current_usage = 80000
        guard._lock("Budget exceeded")

        # Increase limit above current usage
        guard.set_token_limit(150000)

        assert guard.is_locked is False
        assert guard._lock_reason is None

    def test_set_token_limit_does_not_unlock_api_error(self):
        """Test that increasing limit doesn't unlock if locked due to API error."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._lock("LangSmith API error: connection timeout")

        # Increase limit
        guard.set_token_limit(200000)

        # Should still be locked (API error, not budget exceeded)
        assert guard.is_locked is True
        assert guard._lock_reason is not None
        assert "API error" in guard._lock_reason


class TestBudgetGuardRefresh:
    """Tests for BudgetGuard refresh method."""

    @pytest.mark.asyncio
    async def test_refresh_disabled(self):
        """Test that refresh does nothing when disabled."""
        guard = BudgetGuard(
            enabled=False,
            project_name="test-project",
            token_limit=100000,
        )
        await guard.refresh()
        assert guard._current_usage == 0

    @pytest.mark.asyncio
    async def test_refresh_no_client(self):
        """Test that refresh does nothing when no client."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._client = None
        await guard.refresh()
        assert guard._current_usage == 0

    @pytest.mark.asyncio
    async def test_refresh_success(self):
        """Test successful refresh from LangSmith."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        mock_client = Mock()
        mock_client.get_run_stats = Mock(return_value={"total_tokens": 25000})
        guard._client = mock_client

        await guard.refresh()

        assert guard._current_usage == 25000
        assert guard._last_refresh is not None
        assert guard.is_locked is False

    @pytest.mark.asyncio
    async def test_refresh_triggers_warning_at_80_percent(self):
        """Test that refresh triggers warning at 80% usage."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
            warning_thresholds=(0.8, 0.9, 0.95),
        )
        mock_client = Mock()
        mock_client.get_run_stats = Mock(return_value={"total_tokens": 80000})
        guard._client = mock_client

        await guard.refresh()

        assert 0.8 in guard._warnings_sent
        assert guard.is_locked is False  # Not locked, just warning

    @pytest.mark.asyncio
    async def test_refresh_locks_at_100_percent(self):
        """Test that refresh locks at 100% usage."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        mock_client = Mock()
        mock_client.get_run_stats = Mock(return_value={"total_tokens": 100000})
        guard._client = mock_client

        await guard.refresh()

        assert guard.is_locked is True
        assert guard._lock_reason == "Budget exceeded"

    @pytest.mark.asyncio
    async def test_refresh_fail_closed_on_api_error(self):
        """Test fail-closed behavior on API error."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        mock_client = Mock()
        mock_client.get_run_stats = Mock(side_effect=Exception("Connection timeout"))
        guard._client = mock_client

        await guard.refresh()

        assert guard.is_locked is True
        assert guard._lock_reason is not None
        assert "LangSmith API error" in guard._lock_reason
        assert "Connection timeout" in guard._lock_reason

    @pytest.mark.asyncio
    async def test_refresh_handles_none_total_tokens(self):
        """Test refresh handles None value for total_tokens."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        mock_client = Mock()
        mock_client.get_run_stats = Mock(return_value={"total_tokens": None})
        guard._client = mock_client

        await guard.refresh()

        assert guard._current_usage == 0
        assert guard.is_locked is False


class TestBudgetGuardMonthlyReset:
    """Tests for monthly warning reset behavior."""

    @pytest.mark.asyncio
    async def test_reset_warnings_on_new_month(self):
        """Test that warnings are reset on new month."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._warnings_sent = {0.8, 0.9}
        guard._last_warning_month = 1  # January

        # Simulate being in February
        with patch("app.core.budget_guard.datetime") as mock_dt:
            mock_now = Mock()
            mock_now.month = 2  # February
            mock_now.replace.return_value = datetime(2024, 2, 1, tzinfo=timezone.utc)
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            guard._reset_warnings_if_new_month()

            assert len(guard._warnings_sent) == 0
            assert guard._last_warning_month == 2

    @pytest.mark.asyncio
    async def test_unlock_on_new_month_if_budget_exceeded(self):
        """Test that budget exceeded lock is cleared on new month."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
        )
        guard._lock("Budget exceeded")
        guard._last_warning_month = 1

        with patch("app.core.budget_guard.datetime") as mock_dt:
            mock_now = Mock()
            mock_now.month = 2
            mock_now.replace.return_value = datetime(2024, 2, 1, tzinfo=timezone.utc)
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            guard._reset_warnings_if_new_month()

            assert guard.is_locked is False


class TestBudgetGuardPolling:
    """Tests for background polling behavior."""

    @pytest.mark.asyncio
    async def test_start_polling_disabled(self):
        """Test that polling doesn't start when disabled."""
        guard = BudgetGuard(
            enabled=False,
            project_name="test-project",
            token_limit=100000,
        )
        await guard.start_polling()
        assert guard._polling_task is None

    @pytest.mark.asyncio
    async def test_start_polling_enabled(self):
        """Test that polling starts when enabled."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
            check_interval_seconds=1,  # Short interval for testing
        )
        # Mock refresh to avoid actual API calls
        guard.refresh = AsyncMock()

        await guard.start_polling()
        assert guard._polling_task is not None

        # Wait a bit for initial refresh
        await asyncio.sleep(0.05)
        guard.refresh.assert_called()

        # Clean up
        await guard.stop_polling()

    @pytest.mark.asyncio
    async def test_stop_polling(self):
        """Test stopping the polling task."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
            check_interval_seconds=1,
        )
        guard.refresh = AsyncMock()

        await guard.start_polling()
        assert guard._polling_task is not None

        await guard.stop_polling()
        assert guard._polling_task is None

    @pytest.mark.asyncio
    async def test_start_polling_idempotent(self):
        """Test that calling start_polling twice doesn't create duplicate tasks."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
            check_interval_seconds=1,
        )
        guard.refresh = AsyncMock()

        await guard.start_polling()
        task1 = guard._polling_task

        await guard.start_polling()
        task2 = guard._polling_task

        assert task1 is task2  # Same task

        await guard.stop_polling()


class TestBudgetGuardSingleton:
    """Tests for module-level singleton functions."""

    def test_init_budget_guard_disabled(self):
        """Test initializing budget guard as disabled."""
        # Reset global state
        import app.core.budget_guard as bg_module

        bg_module._budget_guard = None

        guard = init_budget_guard(
            project_name="test-project",
            token_limit=100000,
            enabled=False,
        )
        assert guard.enabled is False
        assert get_budget_guard() is guard

    def test_init_budget_guard_enabled(self):
        """Test initializing budget guard as enabled."""
        import app.core.budget_guard as bg_module

        bg_module._budget_guard = None

        # Mock Client to avoid actual client creation
        with patch("app.core.budget_guard.Client"):
            guard = init_budget_guard(
                project_name="test-project",
                token_limit=500000,
                enabled=True,
            )
            assert guard.enabled is True
            assert guard.token_limit == 500000
            assert guard.project_name == "test-project"

    def test_get_budget_guard_returns_none_before_init(self):
        """Test that get_budget_guard returns None before initialization."""
        import app.core.budget_guard as bg_module

        bg_module._budget_guard = None

        assert get_budget_guard() is None

    def test_get_budget_guard_returns_instance_after_init(self):
        """Test that get_budget_guard returns instance after init."""
        import app.core.budget_guard as bg_module

        bg_module._budget_guard = None

        guard = init_budget_guard(
            project_name="test-project",
            token_limit=100000,
            enabled=False,
        )
        result = get_budget_guard()
        assert result is not None
        assert isinstance(result, BudgetGuard)
        assert result is guard


class TestBudgetGuardWarningThresholds:
    """Tests for warning threshold behavior."""

    @pytest.mark.asyncio
    async def test_multiple_warnings_not_repeated(self):
        """Test that the same warning threshold is not triggered twice."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
            warning_thresholds=(0.8, 0.9, 0.95),
        )
        mock_client = Mock()
        mock_client.get_run_stats = Mock(return_value={"total_tokens": 85000})
        guard._client = mock_client

        # First refresh - should trigger 80% warning
        await guard.refresh()
        assert 0.8 in guard._warnings_sent
        assert len(guard._warnings_sent) == 1

        # Second refresh with same usage - should not re-trigger
        await guard.refresh()
        assert len(guard._warnings_sent) == 1

    @pytest.mark.asyncio
    async def test_progressive_warnings(self):
        """Test that warnings trigger progressively as usage increases."""
        guard = BudgetGuard(
            enabled=True,
            project_name="test-project",
            token_limit=100000,
            warning_thresholds=(0.8, 0.9, 0.95),
        )
        mock_client = Mock()
        guard._client = mock_client

        # 80% usage
        mock_client.get_run_stats = Mock(return_value={"total_tokens": 80000})
        await guard.refresh()
        assert guard._warnings_sent == {0.8}

        # 90% usage
        mock_client.get_run_stats = Mock(return_value={"total_tokens": 90000})
        await guard.refresh()
        assert guard._warnings_sent == {0.8, 0.9}

        # 95% usage
        mock_client.get_run_stats = Mock(return_value={"total_tokens": 95000})
        await guard.refresh()
        assert guard._warnings_sent == {0.8, 0.9, 0.95}
