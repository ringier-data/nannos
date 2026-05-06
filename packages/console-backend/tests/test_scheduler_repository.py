"""Unit tests for ScheduledJobRepository helpers.

Tests cover the pure `compute_next_run()` function — no database required.
"""

from datetime import datetime, timedelta, timezone

import pytest
from console_backend.models.scheduled_job import ScheduleKind
from console_backend.repositories.scheduled_job_repository import compute_next_run


class TestComputeNextRun:
    """Tests for compute_next_run() for all ScheduleKind values."""

    def test_cron_advances_to_next_slot(self):
        """Cron schedule returns the next matching datetime after `after`."""
        # Every 5 minutes: */5 * * * *
        after = datetime(2026, 3, 11, 9, 3, 0, tzinfo=timezone.utc)  # 09:03
        result = compute_next_run(
            schedule_kind=ScheduleKind.CRON,
            cron_expr="*/5 * * * *",
            interval_seconds=None,
            run_at=None,
            after=after,
        )
        assert result is not None
        # croniter gives the *next* cron tick strictly after base time
        assert result == datetime(2026, 3, 11, 9, 5, 0, tzinfo=timezone.utc)

    def test_cron_uses_now_when_after_is_none(self):
        """When `after` is None, compute_next_run uses the current time."""
        before = datetime.now(timezone.utc)
        result = compute_next_run(
            schedule_kind=ScheduleKind.CRON,
            cron_expr="0 9 * * *",  # daily 09:00
            interval_seconds=None,
            run_at=None,
            after=None,
        )
        assert result is not None
        # Result must be strictly in the future
        assert result > before

    def test_cron_missing_expr_raises(self):
        """compute_next_run raises AssertionError when cron_expr is None for CRON kind."""
        with pytest.raises(AssertionError):
            compute_next_run(
                schedule_kind=ScheduleKind.CRON,
                cron_expr=None,
                interval_seconds=None,
                run_at=None,
            )

    def test_interval_adds_seconds_to_after(self):
        """Interval schedule returns after + interval_seconds."""
        after = datetime(2026, 3, 11, 10, 0, 0, tzinfo=timezone.utc)
        result = compute_next_run(
            schedule_kind=ScheduleKind.INTERVAL,
            cron_expr=None,
            interval_seconds=3600,  # 1 hour
            run_at=None,
            after=after,
        )
        assert result == after + timedelta(seconds=3600)

    def test_interval_uses_now_when_after_is_none(self):
        """When `after` is None, interval schedule adds seconds to current time."""
        before = datetime.now(timezone.utc)
        result = compute_next_run(
            schedule_kind=ScheduleKind.INTERVAL,
            cron_expr=None,
            interval_seconds=60,
            run_at=None,
            after=None,
        )
        assert result is not None
        assert result > before
        # Should be approximately now + 60s
        assert result <= datetime.now(timezone.utc) + timedelta(seconds=61)

    def test_interval_missing_seconds_raises(self):
        """compute_next_run raises AssertionError when interval_seconds is None for INTERVAL kind."""
        with pytest.raises(AssertionError):
            compute_next_run(
                schedule_kind=ScheduleKind.INTERVAL,
                cron_expr=None,
                interval_seconds=None,
                run_at=None,
            )

    def test_once_returns_none(self):
        """Once schedule returns None — indicates single-run job, no next execution."""
        after = datetime(2026, 4, 1, 8, 0, 0, tzinfo=timezone.utc)
        result = compute_next_run(
            schedule_kind=ScheduleKind.ONCE,
            cron_expr=None,
            interval_seconds=None,
            run_at=after,
            after=after,
        )
        assert result is None

    def test_once_returns_none_regardless_of_run_at(self):
        """Once schedule always returns None even when run_at is in the past."""
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        result = compute_next_run(
            schedule_kind=ScheduleKind.ONCE,
            cron_expr=None,
            interval_seconds=None,
            run_at=past,
        )
        assert result is None
