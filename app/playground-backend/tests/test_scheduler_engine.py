"""Tests for SchedulerEngine.

Mix of:
- Pure unit tests for _parse_result() — no DB or HTTP needed
- DB-backed tests for _heal_stuck_runs() via pg_session
- Mock-based tests for _dispatch_job() and _finalize() business logic
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.scheduled_job import (
    JobRunStatus,
    JobType,
    ScheduledJob,
    ScheduleKind,
)
from playground_backend.repositories.delivery_channel_repository import DeliveryChannelRepository
from playground_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from playground_backend.services.scheduler_engine import SchedulerEngine
from playground_backend.services.scheduler_token_service import SchedulerTokenService


def _make_job(
    job_id: int = 1,
    user_id: str = "user-abc",
    job_type: JobType = JobType.TASK,
    sub_agent_id: int | None = 42,
    schedule_kind: ScheduleKind = ScheduleKind.INTERVAL,
    interval_seconds: int | None = 3600,
    destroy_after_trigger: bool = True,
    max_failures: int = 3,
    consecutive_failures: int = 0,
    delivery_channel_id: int | None = None,
) -> ScheduledJob:
    now = datetime.now(timezone.utc)
    return ScheduledJob(
        id=job_id,
        user_id=user_id,
        sub_agent_id=sub_agent_id,
        name="Test Job",
        job_type=job_type,
        schedule_kind=schedule_kind,
        interval_seconds=interval_seconds,
        next_run_at=now + timedelta(hours=1),
        prompt="Do something",
        destroy_after_trigger=destroy_after_trigger,
        enabled=True,
        max_failures=max_failures,
        consecutive_failures=consecutive_failures,
        delivery_channel_id=delivery_channel_id,
        created_at=now,
        updated_at=now,
    )


def _make_engine(
    *,
    repo: Any = None,
    token_service: Any = None,
    db_session_factory: Any = None,
    socket_manager: Any = None,
) -> SchedulerEngine:
    repo = repo or AsyncMock(spec=ScheduledJobRepository)
    token_service = token_service or AsyncMock(spec=SchedulerTokenService)
    delivery_channel_repo = AsyncMock(spec=DeliveryChannelRepository)
    delivery_channel_repo.get_channel_for_dispatch.return_value = None
    if db_session_factory is None:
        db_session_factory = _make_mock_session_factory()
    return SchedulerEngine(
        repo=repo,
        delivery_channel_repo=delivery_channel_repo,
        token_service=token_service,
        agent_runner_url="http://agent-runner:8000",
        db_session_factory=db_session_factory,
        socket_notification_manager=socket_manager,
    )


def _make_mock_session_factory(session: Any = None):
    """Build an async session factory that yields the given mock session."""
    mock_session = session or AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.execute.return_value = MagicMock(rowcount=0)

    @asynccontextmanager
    async def factory():
        yield mock_session

    return factory


def make_pg_session_factory(pg_session: AsyncSession):
    """Wrap a real pg_session for use as SchedulerEngine session factory."""

    @asynccontextmanager
    async def factory():
        yield pg_session

    return factory


class TestParseResult:
    """Tests for SchedulerEngine._parse_result()."""

    def setup_method(self):
        self.engine = _make_engine()

    def test_rpc_error_returns_failed(self):
        """JSON-RPC error (no 'result') → JobRunStatus.FAILED."""
        data = {"error": {"code": -32603, "message": "Internal error"}}
        status, summary, error_msg, conv_id = self.engine._parse_result(data)

        assert status == JobRunStatus.FAILED
        assert "A2A request error: Internal error" in (error_msg or "")
        assert summary is None

    def test_a2a_task_format_success(self):
        """A2A Task artifact format with scheduler_status=success → SUCCESS."""
        meta = {
            "scheduler_status": "success",
            "agent_message": "Daily report generated.",
        }
        data = {
            "result": {
                "kind": "task",
                "contextId": "ctx-123",
                "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}],
            }
        }
        status, summary, error_msg, conv_id = self.engine._parse_result(data)

        assert status == JobRunStatus.SUCCESS
        assert summary == "Daily report generated."
        assert error_msg is None
        assert conv_id == "ctx-123"

    def test_a2a_task_format_condition_not_met(self):
        """A2A Task artifact with condition_not_met → CONDITION_NOT_MET."""
        meta = {"scheduler_status": "condition_not_met", "agent_message": None}
        data = {
            "result": {
                "kind": "task",
                "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}],
            }
        }
        status, summary, error_msg, conv_id = self.engine._parse_result(data)

        assert status == JobRunStatus.CONDITION_NOT_MET

    def test_a2a_task_format_failed(self):
        """A2A Task artifact with scheduler_status=failed → FAILED."""
        meta = {"scheduler_status": "failed", "error_message": "Tool error"}
        data = {
            "result": {
                "kind": "task",
                "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}],
            }
        }
        status, summary, error_msg, conv_id = self.engine._parse_result(data)

        assert status == JobRunStatus.FAILED
        assert error_msg == "Tool error"

    def test_legacy_format_extracts_from_metadata(self):
        """Legacy format: result.metadata contains scheduler fields directly."""
        data = {
            "result": {
                "metadata": {
                    "scheduler_status": "success",
                    "agent_message": "Done!",
                }
            }
        }
        status, summary, error_msg, conv_id = self.engine._parse_result(data)

        assert status == JobRunStatus.SUCCESS
        assert summary == "Done!"

    def test_missing_scheduler_status_defaults_to_success(self):
        """When scheduler_status is absent, defaults to success."""
        data = {"result": {"metadata": {"agent_message": "something happened"}}}
        status, summary, _, _ = self.engine._parse_result(data)

        assert status == JobRunStatus.SUCCESS

    def test_unknown_status_string_defaults_to_success(self):
        """Unrecognised scheduler_status string falls back to SUCCESS."""
        data = {"result": {"metadata": {"scheduler_status": "unknown_status_xyz"}}}
        status, _, _, _ = self.engine._parse_result(data)

        assert status == JobRunStatus.SUCCESS

    def test_task_state_failed_fallback(self):
        """When artifact has no scheduler_status, task.status.state=failed → FAILED."""
        data = {
            "result": {
                "kind": "task",
                "status": {"state": "failed"},
                "artifacts": [],
            }
        }
        status, _, _, _ = self.engine._parse_result(data)

        assert status == JobRunStatus.FAILED


class TestHealStuckRuns:
    """Tests for SchedulerEngine._heal_stuck_runs() using pg_session."""

    @pytest.mark.asyncio
    async def test_marks_old_running_runs_as_failed(self, pg_session: AsyncSession):
        """Runs stuck in 'running' for >10 min are marked 'failed'."""
        # Insert prerequisite: user + job
        user_id = "heal-user-1"
        await pg_session.execute(
            text(
                "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) VALUES (:id, :sub, :email, :fn, :ln, false, 'member', 'active')"
            ),
            {"id": user_id, "sub": "heal-sub-1", "email": "heal1@test.com", "fn": "Heal", "ln": "Test"},
        )
        result = await pg_session.execute(
            text("""
                INSERT INTO scheduled_jobs
                    (user_id, name, job_type, schedule_kind, interval_seconds, next_run_at, enabled, max_failures, consecutive_failures, destroy_after_trigger, check_tool, condition_expr)
                VALUES
                    (:uid, 'Heal Job', 'watch', 'interval', 3600, NOW() + INTERVAL '1 hour', true, 3, 0, true, 'ping_tool', 'result > 0')
                RETURNING id
            """),
            {"uid": user_id},
        )
        job_id = result.mappings().first()["id"]

        # Insert a run that started 20 minutes ago (stuck)
        stale_started = datetime.now(timezone.utc) - timedelta(minutes=20)
        result = await pg_session.execute(
            text("""
                INSERT INTO scheduled_job_runs (job_id, started_at, status)
                VALUES (:job_id, :started_at, 'running')
                RETURNING id
            """),
            {"job_id": job_id, "started_at": stale_started},
        )
        stale_run_id = result.mappings().first()["id"]

        # Insert a fresh run (only 1 minute ago — should not be healed)
        fresh_started = datetime.now(timezone.utc) - timedelta(minutes=1)
        result = await pg_session.execute(
            text("""
                INSERT INTO scheduled_job_runs (job_id, started_at, status)
                VALUES (:job_id, :started_at, 'running')
                RETURNING id
            """),
            {"job_id": job_id, "started_at": fresh_started},
        )
        fresh_run_id = result.mappings().first()["id"]
        await pg_session.commit()

        engine = _make_engine(db_session_factory=make_pg_session_factory(pg_session))

        await engine._heal_stuck_runs()

        # Stale run should now be 'failed'
        r = await pg_session.execute(text("SELECT status FROM scheduled_job_runs WHERE id = :id"), {"id": stale_run_id})
        assert r.scalar_one() == "failed"

        # Fresh run should remain 'running'
        r = await pg_session.execute(text("SELECT status FROM scheduled_job_runs WHERE id = :id"), {"id": fresh_run_id})
        assert r.scalar_one() == "running"

    @pytest.mark.asyncio
    async def test_heal_does_not_touch_completed_runs(self, pg_session: AsyncSession):
        """Completed runs are not affected by healing."""
        user_id = "heal-user-2"
        await pg_session.execute(
            text(
                "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) VALUES (:id, :sub, :email, :fn, :ln, false, 'member', 'active')"
            ),
            {"id": user_id, "sub": "heal-sub-2", "email": "heal2@test.com", "fn": "Heal", "ln": "Two"},
        )
        result = await pg_session.execute(
            text("""
                INSERT INTO scheduled_jobs
                    (user_id, name, job_type, schedule_kind, interval_seconds, next_run_at, enabled, max_failures, consecutive_failures, destroy_after_trigger, check_tool, condition_expr)
                VALUES
                    (:uid, 'Heal Job 2', 'watch', 'interval', 3600, NOW() + INTERVAL '1 hour', true, 3, 0, true, 'ping_tool', 'result > 0')
                RETURNING id
            """),
            {"uid": user_id},
        )
        job_id = result.mappings().first()["id"]

        stale_started = datetime.now(timezone.utc) - timedelta(minutes=30)
        result = await pg_session.execute(
            text("""
                INSERT INTO scheduled_job_runs (job_id, started_at, completed_at, status)
                VALUES (:job_id, :started_at, NOW(), 'success')
                RETURNING id
            """),
            {"job_id": job_id, "started_at": stale_started},
        )
        old_success_run_id = result.mappings().first()["id"]
        await pg_session.commit()

        engine = _make_engine(db_session_factory=make_pg_session_factory(pg_session))
        await engine._heal_stuck_runs()

        # Old success run should remain 'success'
        r = await pg_session.execute(
            text("SELECT status FROM scheduled_job_runs WHERE id = :id"), {"id": old_success_run_id}
        )
        assert r.scalar_one() == "success"


class TestDispatchJobNoToken:
    """When _token_service raises ValueError (no offline token), job is auto-paused."""

    @pytest.mark.asyncio
    async def test_auto_pauses_when_no_offline_token(self):
        """dispatch_job() auto-pauses the job when SchedulerTokenService raises ValueError."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 1
        repo.complete_job = AsyncMock()
        repo.complete_run = AsyncMock()

        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.side_effect = ValueError("No offline token stored")

        engine = _make_engine(repo=repo, token_service=token_service)
        job = _make_job()

        await engine._dispatch_job(job)

        # complete_run called with FAILED status
        repo.complete_run.assert_awaited_once()
        call_kwargs = repo.complete_run.call_args[1]
        assert call_kwargs["status"] == JobRunStatus.FAILED

        # complete_job called with paused_reason explaining missing token
        repo.complete_job.assert_awaited_once()
        call_kwargs = repo.complete_job.call_args[1]
        assert call_kwargs["paused_reason"] is not None
        assert "offline token" in (call_kwargs["paused_reason"] or "").lower() or "No offline token" in (
            call_kwargs["paused_reason"] or ""
        )


class TestFinalizeJobState:
    """Tests for SchedulerEngine._finalize() business logic using mocked repo."""

    @pytest.mark.asyncio
    async def test_once_job_disabled_after_success(self):
        """A once-only job (schedule_kind=ONCE) has no next_run_at, so enabled=False after success."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()

        engine = _make_engine(repo=repo)

        now = datetime.now(timezone.utc)
        once_job = ScheduledJob(
            id=10,
            user_id="u1",
            name="Once Job",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.ONCE,
            run_at=now - timedelta(minutes=1),
            next_run_at=now,
            enabled=True,
            max_failures=3,
            consecutive_failures=0,
            destroy_after_trigger=False,
            created_at=now,
            updated_at=now,
        )

        await engine._finalize(run_id=1, job=once_job, status=JobRunStatus.SUCCESS)

        repo.complete_job.assert_awaited_once()
        kwargs = repo.complete_job.call_args[1]
        # Once job: compute_next_run returns None → next_run_at=None → disabled
        assert kwargs["next_run_at"] is None
        assert kwargs["success"] is True

    @pytest.mark.asyncio
    async def test_failure_increments_passed_to_repo(self):
        """On failure, success=False is passed so the repo can increment consecutive_failures."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()

        engine = _make_engine(repo=repo)
        interval_job = _make_job(schedule_kind=ScheduleKind.INTERVAL, interval_seconds=300)

        await engine._finalize(run_id=5, job=interval_job, status=JobRunStatus.FAILED, error_message="Oops")

        kwargs = repo.complete_job.call_args[1]
        assert kwargs["success"] is False

    @pytest.mark.asyncio
    async def test_condition_not_met_counts_as_success(self):
        """CONDITION_NOT_MET is treated as success (no failure increment)."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()

        engine = _make_engine(repo=repo)
        watch_job = _make_job(job_type=JobType.WATCH, schedule_kind=ScheduleKind.INTERVAL, interval_seconds=60)

        await engine._finalize(run_id=2, job=watch_job, status=JobRunStatus.CONDITION_NOT_MET)

        kwargs = repo.complete_job.call_args[1]
        assert kwargs["success"] is True

    @pytest.mark.asyncio
    async def test_destroy_after_trigger_disables_watch_job(self):
        """Watch job with destroy_after_trigger=True is disabled via SQL after SUCCESS."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.execute = AsyncMock()

        @asynccontextmanager
        async def factory():
            yield mock_db

        engine = _make_engine(repo=repo, db_session_factory=factory)

        watch_job = _make_job(
            job_type=JobType.WATCH,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=60,
            destroy_after_trigger=True,
        )
        await engine._finalize(run_id=3, job=watch_job, status=JobRunStatus.SUCCESS)

        # db.execute must have been called with an UPDATE that disables the job
        execute_calls = mock_db.execute.call_args_list
        sql_calls = [str(c.args[0]) for c in execute_calls if c.args]
        assert any("enabled = FALSE" in sql for sql in sql_calls), (
            "Expected UPDATE … SET enabled = FALSE not found in execute calls"
        )

    @pytest.mark.asyncio
    async def test_watch_job_without_destroy_after_trigger_stays_enabled(self):
        """Watch job with destroy_after_trigger=False stays enabled after SUCCESS."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.execute = AsyncMock()

        @asynccontextmanager
        async def factory():
            yield mock_db

        engine = _make_engine(repo=repo, db_session_factory=factory)

        watch_job = _make_job(
            job_type=JobType.WATCH,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=60,
            destroy_after_trigger=False,
        )
        await engine._finalize(run_id=4, job=watch_job, status=JobRunStatus.SUCCESS)

        execute_calls = mock_db.execute.call_args_list
        sql_calls = [str(c.args[0]) for c in execute_calls if c.args]
        assert not any("enabled = FALSE" in sql for sql in sql_calls), (
            "Should NOT disable a watch job when destroy_after_trigger=False"
        )

    @pytest.mark.asyncio
    async def test_paused_reason_forwarded_to_repo(self):
        """paused_reason is forwarded to complete_job so the repo can persist it."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()

        engine = _make_engine(repo=repo)
        interval_job = _make_job()

        reason = "No offline token stored. User must re-grant scheduler consent."
        await engine._finalize(
            run_id=7,
            job=interval_job,
            status=JobRunStatus.FAILED,
            paused_reason=reason,
        )

        kwargs = repo.complete_job.call_args[1]
        assert kwargs["paused_reason"] == reason

    @pytest.mark.asyncio
    async def test_websocket_notification_sent_when_manager_present(self):
        """WebSocket notification is sent when socket_notification_manager is provided."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()

        socket_manager = AsyncMock()
        socket_manager.send_notification = AsyncMock(return_value=True)

        engine = _make_engine(repo=repo, socket_manager=socket_manager)
        job = _make_job(user_id="notify-user")

        await engine._finalize(run_id=8, job=job, status=JobRunStatus.SUCCESS)

        socket_manager.send_notification.assert_awaited_once()
        call_args = socket_manager.send_notification.call_args
        assert call_args[0][0] == "notify-user"  # correct user_id
