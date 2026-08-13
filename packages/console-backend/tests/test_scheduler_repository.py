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

    def test_cron_respects_timezone_summer(self):
        """'0 8 * * *' with tz=Europe/Zurich fires at 08:00 CEST (06:00 UTC) during DST."""
        after = datetime(2026, 6, 10, 5, 0, 0, tzinfo=timezone.utc)  # 07:00 CEST
        result = compute_next_run(
            schedule_kind=ScheduleKind.CRON,
            cron_expr="0 8 * * *",
            interval_seconds=None,
            run_at=None,
            after=after,
            tz="Europe/Zurich",
        )
        assert result == datetime(2026, 6, 10, 6, 0, 0, tzinfo=timezone.utc)

    def test_cron_respects_timezone_winter(self):
        """'0 8 * * *' with tz=Europe/Zurich fires at 08:00 CET (07:00 UTC) outside DST."""
        after = datetime(2026, 1, 10, 5, 0, 0, tzinfo=timezone.utc)  # 06:00 CET
        result = compute_next_run(
            schedule_kind=ScheduleKind.CRON,
            cron_expr="0 8 * * *",
            interval_seconds=None,
            run_at=None,
            after=after,
            tz="Europe/Zurich",
        )
        assert result == datetime(2026, 1, 10, 7, 0, 0, tzinfo=timezone.utc)

    def test_cron_without_tz_uses_utc_when_no_default_configured(self, monkeypatch: pytest.MonkeyPatch):
        """tz=None resolves to DEFAULT_TIMEZONE, which falls back to UTC when unset."""
        monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
        after = datetime(2026, 6, 10, 5, 0, 0, tzinfo=timezone.utc)
        result = compute_next_run(
            schedule_kind=ScheduleKind.CRON,
            cron_expr="0 8 * * *",
            interval_seconds=None,
            run_at=None,
            after=after,
        )
        assert result == datetime(2026, 6, 10, 8, 0, 0, tzinfo=timezone.utc)

    def test_cron_without_tz_follows_default_timezone_env(self, monkeypatch: pytest.MonkeyPatch):
        """tz=None (and empty string) follow the deployment's DEFAULT_TIMEZONE env var."""
        monkeypatch.setenv("DEFAULT_TIMEZONE", "Europe/Zurich")
        after = datetime(2026, 6, 10, 5, 0, 0, tzinfo=timezone.utc)  # 07:00 CEST
        for tz in (None, "", "  "):
            result = compute_next_run(
                schedule_kind=ScheduleKind.CRON,
                cron_expr="0 8 * * *",
                interval_seconds=None,
                run_at=None,
                after=after,
                tz=tz,
            )
            assert result == datetime(2026, 6, 10, 6, 0, 0, tzinfo=timezone.utc)

    def test_cron_invalid_tz_raises_value_error(self):
        """Unresolvable names raise ValueError (not ZoneInfoNotFoundError, a KeyError subclass)."""
        with pytest.raises(ValueError, match="Unknown IANA timezone"):
            compute_next_run(
                schedule_kind=ScheduleKind.CRON,
                cron_expr="0 8 * * *",
                interval_seconds=None,
                run_at=None,
                tz="Zurich",
            )

    def test_cron_dst_fall_back_fires_once(self):
        """2026-10-25 Europe/Zurich: 02:30 exists twice (CEST and CET); the job must fire once.

        croniter yields both folds — after the fold-0 fire (00:30 UTC) the recompute
        must skip the fold-1 repeat (01:30 UTC) and land on the next day.
        """
        zone_day = datetime(2026, 10, 24, 23, 0, 0, tzinfo=timezone.utc)
        first = compute_next_run(
            schedule_kind=ScheduleKind.CRON,
            cron_expr="30 2 * * *",
            interval_seconds=None,
            run_at=None,
            after=zone_day,
            tz="Europe/Zurich",
        )
        assert first == datetime(2026, 10, 25, 0, 30, 0, tzinfo=timezone.utc)  # 02:30+02:00

        after_first_fire = first + timedelta(minutes=1)
        second = compute_next_run(
            schedule_kind=ScheduleKind.CRON,
            cron_expr="30 2 * * *",
            interval_seconds=None,
            run_at=None,
            after=after_first_fire,
            tz="Europe/Zurich",
        )
        assert second == datetime(2026, 10, 26, 1, 30, 0, tzinfo=timezone.utc)  # next day, 02:30+01:00

    def test_cron_dst_spring_forward_maps_to_existing_time(self):
        """2026-03-29 Europe/Zurich: 02:30 does not exist; croniter maps it forward once."""
        after = datetime(2026, 3, 28, 23, 0, 0, tzinfo=timezone.utc)
        result = compute_next_run(
            schedule_kind=ScheduleKind.CRON,
            cron_expr="30 2 * * *",
            interval_seconds=None,
            run_at=None,
            after=after,
            tz="Europe/Zurich",
        )
        assert result == datetime(2026, 3, 29, 1, 0, 0, tzinfo=timezone.utc)  # 03:00+02:00

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
