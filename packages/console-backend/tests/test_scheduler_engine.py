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
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from console_backend.models.scheduled_job import (
    JobRunStatus,
    JobType,
    ScheduledJob,
    ScheduleKind,
)
from console_backend.repositories.delivery_channel_repository import DeliveryChannelRepository
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from console_backend.services.watch_evaluator import WatchOutcome
from console_backend.services.scheduler_engine import SchedulerEngine
from console_backend.services.scheduler_token_service import SchedulerTokenService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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
        """Runs stuck in 'running' past STUCK_RUN_THRESHOLD are marked 'failed'."""
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
                    (user_id, name, job_type, schedule_kind, interval_seconds, next_run_at, enabled, max_failures, consecutive_failures, destroy_after_trigger, check_tool, cel_expr)
                VALUES
                    (:uid, 'Heal Job', 'watch', 'interval', 3600, NOW() + INTERVAL '1 hour', true, 3, 0, true, 'ping_tool', 'result != null')
                RETURNING id
            """),
            {"uid": user_id},
        )
        job_id = result.mappings().first()["id"]

        # Insert a run that started well past the threshold (stuck)
        stale_started = datetime.now(timezone.utc) - timedelta(minutes=45)
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
                    (user_id, name, job_type, schedule_kind, interval_seconds, next_run_at, enabled, max_failures, consecutive_failures, destroy_after_trigger, check_tool, cel_expr)
                VALUES
                    (:uid, 'Heal Job 2', 'watch', 'interval', 3600, NOW() + INTERVAL '1 hour', true, 3, 0, true, 'ping_tool', 'result != null')
                RETURNING id
            """),
            {"uid": user_id},
        )
        job_id = result.mappings().first()["id"]

        stale_started = datetime.now(timezone.utc) - timedelta(minutes=45)
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

    @pytest.mark.asyncio
    async def test_in_flight_run_is_never_healed(self, pg_session: AsyncSession):
        """A dispatch this process is still running is not stuck, however long it takes.

        The healer runs on every tick now, so without this exclusion a slow agent would
        have its own run marked failed underneath it — and then overwrite that verdict
        when it finished.
        """
        user_id = "heal-user-3"
        await pg_session.execute(
            text(
                "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) VALUES (:id, :sub, :email, :fn, :ln, false, 'member', 'active')"
            ),
            {"id": user_id, "sub": "heal-sub-3", "email": "heal3@test.com", "fn": "Heal", "ln": "Three"},
        )
        result = await pg_session.execute(
            text("""
                INSERT INTO scheduled_jobs
                    (user_id, name, job_type, schedule_kind, interval_seconds, next_run_at, enabled, max_failures, consecutive_failures, destroy_after_trigger, check_tool, cel_expr)
                VALUES
                    (:uid, 'Heal Job 3', 'watch', 'interval', 3600, NOW() + INTERVAL '1 hour', true, 3, 0, true, 'ping_tool', 'result != null')
                RETURNING id
            """),
            {"uid": user_id},
        )
        job_id = result.mappings().first()["id"]

        long_started = datetime.now(timezone.utc) - timedelta(hours=3)
        result = await pg_session.execute(
            text("""
                INSERT INTO scheduled_job_runs (job_id, started_at, status)
                VALUES (:job_id, :started_at, 'running')
                RETURNING id
            """),
            {"job_id": job_id, "started_at": long_started},
        )
        live_run_id = result.mappings().first()["id"]
        await pg_session.commit()

        engine = _make_engine(db_session_factory=make_pg_session_factory(pg_session))
        engine._in_flight.add(live_run_id)

        await engine._heal_stuck_runs()

        r = await pg_session.execute(
            text("SELECT status FROM scheduled_job_runs WHERE id = :id"), {"id": live_run_id}
        )
        assert r.scalar_one() == "running"


class TestFinalizeAdvancesDespiteRunWriteFailure:
    """The schedule must advance even when the run record cannot be written.

    This is the shape of a real outage: complete_run failed on a column the deployed
    schema did not have, which rolled back the shared transaction and left next_run_at
    in the past — so claim_due_jobs re-claimed the job every tick and the check tool was
    called hundreds of times. Advancing first makes the loop impossible.
    """

    @pytest.mark.asyncio
    async def test_run_write_failure_does_not_block_schedule_advance(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_job = AsyncMock()
        repo.complete_run = AsyncMock(side_effect=RuntimeError("column does not exist"))

        engine = _make_engine(repo=repo)
        job = _make_job(job_id=77, schedule_kind=ScheduleKind.INTERVAL, interval_seconds=3600)

        # Must not raise: the caller has nothing useful to do with a bookkeeping failure.
        await engine._finalize(run_id=99, job=job, status=JobRunStatus.SUCCESS)

        repo.complete_job.assert_awaited_once()
        kwargs = repo.complete_job.call_args[1]
        assert kwargs["next_run_at"] is not None
        assert kwargs["next_run_at"] > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_schedule_advances_before_the_run_is_recorded(self):
        """Ordering, not just independence — the advance cannot be the write's hostage."""
        calls: list[str] = []
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_job = AsyncMock(side_effect=lambda **_: calls.append("job"))
        repo.complete_run = AsyncMock(side_effect=lambda **_: calls.append("run"))

        engine = _make_engine(repo=repo)
        await engine._finalize(run_id=1, job=_make_job(), status=JobRunStatus.SUCCESS)

        assert calls == ["job", "run"]


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


class TestDispatchErrorHandling:
    """A dispatch error from agent-runner must finalize the run as FAILED (not leave it stuck).

    The transport is the native a2a-sdk client (dispatch_streaming); we assert the engine's
    error handling at that seam rather than the old hand-rolled SSE.
    """

    @pytest.mark.asyncio
    async def test_dispatch_records_failure_on_http_error(self):
        """An HTTP error from agent-runner must be finalized as FAILED with the status code
        and body surfaced in the error message."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run = AsyncMock(return_value=99)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token = AsyncMock(return_value="token-xyz")

        engine = _make_engine(repo=repo, token_service=token_service)

        http_error = httpx.HTTPStatusError(
            "404",
            request=httpx.Request("POST", "http://agent-runner:8000/"),
            response=httpx.Response(404, text="Not Found"),
        )
        with patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            new=AsyncMock(side_effect=http_error),
        ):
            await engine._dispatch_job(_make_job(), run_id=99)

        repo.complete_run.assert_awaited_once()
        kwargs = repo.complete_run.await_args.kwargs
        assert kwargs["status"] == JobRunStatus.FAILED
        assert "404" in (kwargs["error_message"] or "")
        assert "Not Found" in (kwargs["error_message"] or "")

    @pytest.mark.asyncio
    async def test_dispatch_records_failure_on_generic_error(self):
        """A non-HTTP dispatch error (e.g. transport/JSON-RPC) must still finalize FAILED."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run = AsyncMock(return_value=99)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token = AsyncMock(return_value="token-xyz")

        engine = _make_engine(repo=repo, token_service=token_service)

        with patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            new=AsyncMock(side_effect=RuntimeError("assessor exploded")),
        ):
            await engine._dispatch_job(_make_job(), run_id=99)

        repo.complete_run.assert_awaited_once()
        kwargs = repo.complete_run.await_args.kwargs
        assert kwargs["status"] == JobRunStatus.FAILED
        assert "assessor exploded" in (kwargs["error_message"] or "")


class TestFinalizeInvalidTimezone:
    """An unresolvable stored timezone must pause the job, not crash _finalize.

    If _finalize raised instead, next_run_at would stay in the past and
    claim_due_jobs would re-claim and re-execute the job on every tick.
    """

    @pytest.mark.asyncio
    async def test_invalid_timezone_pauses_job(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock()

        engine = _make_engine(repo=repo)
        job = _make_job(schedule_kind=ScheduleKind.CRON, interval_seconds=None)
        job.cron_expr = "0 8 * * *"
        job.timezone = "Zurich"  # migrated verbatim from unvalidated user settings

        await engine._finalize(run_id=1, job=job, status=JobRunStatus.SUCCESS)

        repo.complete_run.assert_awaited_once()
        repo.complete_job.assert_awaited_once()
        kwargs = repo.complete_job.call_args[1]
        # next_run_at=None disables the job; the reason tells the user how to recover.
        assert kwargs["next_run_at"] is None
        assert "Invalid timezone" in kwargs["paused_reason"]
        assert "Zurich" in kwargs["paused_reason"]



class TestBuildMessageArgs:
    """What a dispatch carries now that agent-runner knows nothing about watches.

    The sub-agent instruction used to travel in metadata["watch"]["prompt"] and be
    reassembled there. The scheduler builds the whole prompt now, so it travels in the
    text part like any other job's.
    """

    @pytest.mark.asyncio
    async def test_a_triggered_watch_with_an_agent_carries_instruction_and_result(self):
        engine = _make_engine()
        job = _make_job(job_type=JobType.WATCH)
        job.check_tool = "ping_tool"
        job.cel_expr = "result.status != ''"
        job.timezone = "Europe/Zurich"

        parts, metadata, push_config = await engine._build_message_args(
            job,
            run_id=7,
            access_token="tok",
            db=AsyncMock(),
            watch_outcome=WatchOutcome(condition_met=True, check_result={"status": "FAILED"}),
        )

        text = parts[0]["text"]
        assert "Do something" in text  # the job's instruction
        assert '"status": "FAILED"' in text  # and what triggered it
        assert metadata["sub_agent_id"] == 42
        assert metadata["timezone"] == "Europe/Zurich"
        assert "watch" not in metadata  # the contract is gone
        assert push_config is None

    @pytest.mark.asyncio
    async def test_an_agent_without_an_instruction_gets_a_default(self):
        engine = _make_engine()
        job = _make_job(job_type=JobType.WATCH)
        job.prompt = ""
        job.check_tool = "ping_tool"

        parts, _, _ = await engine._build_message_args(
            job,
            run_id=7,
            access_token="tok",
            db=AsyncMock(),
            watch_outcome=WatchOutcome(condition_met=True, check_result={"a": 1}),
        )
        assert "Take appropriate action based on the check result" in parts[0]["text"]

    @pytest.mark.asyncio
    async def test_a_notification_only_watch_carries_the_written_message(self):
        engine = _make_engine()
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None)
        job.check_tool = "ping_tool"
        job.notification_message = "Sync broke again."

        parts, metadata, _ = await engine._build_message_args(
            job,
            run_id=7,
            access_token="tok",
            db=AsyncMock(),
            watch_outcome=WatchOutcome(condition_met=True, check_result={"a": 1}),
        )
        assert parts == [{"kind": "text", "text": "Sync broke again."}]
        assert "sub_agent_id" not in metadata

    @pytest.mark.asyncio
    async def test_an_empty_notification_is_written_here(self):
        # It used to be written inside the agent run, which is why a watch that only
        # notifies needed an agent at all.
        engine = _make_engine()
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None)
        job.check_tool = "ping_tool"
        job.notification_message = ""

        with patch.object(engine, "_write_notification", AsyncMock(return_value="Written here.")):
            parts, _, _ = await engine._build_message_args(
                job,
                run_id=7,
                access_token="tok",
                db=AsyncMock(),
                watch_outcome=WatchOutcome(condition_met=True, check_result={"a": 1}),
            )
        assert parts == [{"kind": "text", "text": "Written here."}]

    @pytest.mark.asyncio
    async def test_the_delivery_channel_decides_how_the_run_writes(self):
        """A Slack-bound job dispatches with the channel's rendering rules.

        Nothing between the agent and Slack rewrites its output, so a run that is not
        told the channel renders mrkdwn produces '### heading' / '**bold**' and the user
        reads the syntax. The rules live on the channel and ride the same metadata key an
        interactive client sends, so the run obeys them either way.
        """
        engine = _make_engine()
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = {
            "webhook_url": "https://slack.example/callback",
            "secret": "s3cret",
            "message_formatting": "slack",
        }
        job = _make_job(delivery_channel_id=3)

        _, metadata, push_config = await engine._build_message_args(
            job, run_id=7, access_token="tok", db=AsyncMock()
        )

        assert metadata["messageFormatting"] == "slack"
        # The channel is fetched once and still yields the push target.
        assert push_config == {"url": "https://slack.example/callback", "token": "s3cret"}
        assert engine._delivery_channel_repo.get_channel_for_dispatch.await_count == 1

    @pytest.mark.asyncio
    async def test_a_job_without_a_channel_writes_markdown(self):
        engine = _make_engine()
        job = _make_job(delivery_channel_id=None)

        _, metadata, push_config = await engine._build_message_args(
            job, run_id=7, access_token="tok", db=AsyncMock()
        )

        assert metadata["messageFormatting"] == "markdown"
        assert push_config is None

    @pytest.mark.asyncio
    async def test_a_voice_call_is_not_told_how_to_render_text(self):
        """Nothing is rendered on a phone call, so text rules have no business there."""
        engine = _make_engine()
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = {
            "webhook_url": "https://slack.example/callback",
            "secret": "s3cret",
            "message_formatting": "slack",
        }
        job = _make_job(delivery_channel_id=3).model_copy(update={"voice_call": True})

        with patch.object(engine, "_resolve_voice_agent_id", AsyncMock(return_value=77)):
            _, metadata, _ = await engine._build_message_args(
                job, run_id=7, access_token="tok", db=AsyncMock()
            )

        assert "messageFormatting" not in metadata

    @pytest.mark.asyncio
    async def test_a_voice_job_that_finds_no_voice_agent_still_formats_its_text(self):
        """The degraded path dispatches text, and that text still lands on the channel."""
        engine = _make_engine()
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = {
            "webhook_url": "https://slack.example/callback",
            "secret": "s3cret",
            "message_formatting": "slack",
        }
        job = _make_job(delivery_channel_id=3).model_copy(update={"voice_call": True})

        with patch.object(engine, "_resolve_voice_agent_id", AsyncMock(return_value=None)):
            _, metadata, _ = await engine._build_message_args(
                job, run_id=7, access_token="tok", db=AsyncMock()
            )

        assert metadata["messageFormatting"] == "slack"


class TestWatchEvaluatedBeforeDispatch:
    """A watch's condition is decided here, before anything is dispatched.

    The point of moving it: a poll that does not trigger costs no agent run, and a poll
    that does can choose its target — which is what makes a voice-call watch possible.
    """

    @staticmethod
    def _watch_job(**overrides) -> ScheduledJob:
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None, **overrides)
        return job.model_copy(
            update={
                "check_tool": "naonous_get_campaign",
                "cel_expr": "eq_ci(result.status, 'FAILED')",
            }
        )

    @pytest.mark.asyncio
    async def test_an_unmet_condition_dispatches_nothing(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 7
        engine = _make_engine(repo=repo)

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=False, check_result={"status": "OK"})),
        ):
            with patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
                await engine._dispatch_job(self._watch_job())

        dispatch.assert_not_called()
        kwargs = repo.complete_run.call_args[1]
        assert kwargs["status"] == JobRunStatus.CONDITION_NOT_MET

    @pytest.mark.asyncio
    async def test_a_check_failure_is_a_failed_run_not_a_quiet_one(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 8
        engine = _make_engine(repo=repo)

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=False, error="gateway unreachable")),
        ):
            with patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
                await engine._dispatch_job(self._watch_job())

        dispatch.assert_not_called()
        kwargs = repo.complete_run.call_args[1]
        assert kwargs["status"] == JobRunStatus.FAILED

    @pytest.mark.asyncio
    async def test_a_met_condition_dispatches_with_the_verdict_attached(self):
        # The runner must not call the tool again or reach a different answer.
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 9
        engine = _make_engine(repo=repo)
        result = {"status": "FAILED"}

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=True, check_result=result)),
        ):
            with patch(
                "console_backend.services.scheduler_engine.dispatch_streaming",
                AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
            ) as dispatch:
                await engine._dispatch_job(self._watch_job())

        # The verdict no longer travels: the runner never second-guesses it, so what
        # matters is that the dispatch carries what to act on.
        call = dispatch.await_args[1]
        assert "watch" not in call["metadata"]
        assert '"status": "FAILED"' in call["parts"][0]["text"]

    @pytest.mark.asyncio
    async def test_console_served_tools_are_evaluated_here_too(self):
        # They were the last case the runner still decided, which is why it kept a copy
        # of the evaluation. The tool client reaches this backend's own /mcp mount now.
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 10
        engine = _make_engine(repo=repo)
        job = self._watch_job().model_copy(update={"check_tool": "console_list_mcp_tools"})

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=True, check_result={"tools": []})),
        ) as evaluate:
            with patch(
                "console_backend.services.scheduler_engine.dispatch_streaming",
                AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
            ) as dispatch:
                await engine._dispatch_job(job)

        evaluate.assert_awaited_once()
        assert "watch" not in dispatch.await_args[1]["metadata"]

    @pytest.mark.asyncio
    async def test_a_triggered_watch_can_be_dispatched_as_a_voice_call(self):
        # This is the payoff: the target is chosen after the condition is known, so the
        # phone only rings because something happened.
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 11
        engine = _make_engine(repo=repo)
        job = self._watch_job().model_copy(update={"voice_call": True, "sub_agent_id": 42})

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=True, check_result={"status": "FAILED"})),
        ):
            with patch.object(engine, "_resolve_voice_agent_id", AsyncMock(return_value=99)):
                with patch(
                    "console_backend.services.scheduler_engine.dispatch_streaming",
                    AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
                ) as dispatch:
                    await engine._dispatch_job(job)

        call = dispatch.await_args[1]
        assert call["metadata"]["sub_agent_id"] == 99
        assert any(part.get("kind") == "data" for part in call["parts"])


class TestVoiceCallDispatch:
    """A voice call is a delivery choice, not a job-type capability.

    It was task-only while agent-runner evaluated watch conditions: dispatch preceded the
    verdict, so a voice watch would have rung on every poll. The scheduler decides first
    now, so a call only happens because something happened.
    """

    @staticmethod
    def _watch_job(**overrides) -> ScheduledJob:
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None, **overrides)
        return job.model_copy(
            update={"check_tool": "naonous_get_campaign", "cel_expr": "result.status", "voice_call": True}
        )

    async def _dispatch(self, engine, job, outcome: WatchOutcome | None):
        patched_eval = (
            patch.object(engine._watch_evaluator, "evaluate", AsyncMock(return_value=outcome))
            if outcome
            else patch.object(engine._watch_evaluator, "can_evaluate", MagicMock(return_value=False))
        )
        with patched_eval:
            with patch.object(engine, "_resolve_voice_agent_id", AsyncMock(return_value=99)):
                with patch(
                    "console_backend.services.scheduler_engine.dispatch_streaming",
                    AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
                ) as dispatch:
                    await engine._dispatch_job(job)
        return dispatch.await_args[1]

    @pytest.mark.asyncio
    async def test_a_notify_only_watch_can_be_a_call(self):
        # No sub-agent to borrow config from, so the call needs a system prompt of its
        # own — and an empty DataPart is rejected by the voice agent outright.
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 20
        engine = _make_engine(repo=repo)
        call = await self._dispatch(
            engine,
            self._watch_job(),
            WatchOutcome(condition_met=True, check_result={"status": "FAILED"}),
        )

        assert call["metadata"]["sub_agent_id"] == 99  # dispatched to the voice agent
        data = next(p for p in call["parts"] if p.get("kind") == "data")["data"]
        assert data.get("sub_agent_id") is None
        assert "system_prompt" in data
        # The call has something to report: the result is injected as session context,
        # because the runner path that would have written a message is not taken here.
        assert any("Check result" in p.get("text", "") for p in call["parts"])

    @pytest.mark.asyncio
    async def test_a_watch_with_a_sub_agent_borrows_its_config(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 21
        engine = _make_engine(repo=repo)
        call = await self._dispatch(
            engine,
            self._watch_job().model_copy(update={"sub_agent_id": 42}),
            WatchOutcome(condition_met=True, check_result={"status": "FAILED"}),
        )

        data = next(p for p in call["parts"] if p.get("kind") == "data")["data"]
        assert data["sub_agent_id"] == 42
        assert "system_prompt" not in data

    @pytest.mark.asyncio
    async def test_a_task_still_dispatches_as_a_call(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 22
        engine = _make_engine(repo=repo)
        job = _make_job(job_type=JobType.TASK, sub_agent_id=42).model_copy(update={"voice_call": True})
        call = await self._dispatch(engine, job, None)

        assert call["metadata"]["sub_agent_id"] == 99
        assert next(p for p in call["parts"] if p.get("kind") == "data")["data"]["sub_agent_id"] == 42

    @pytest.mark.asyncio
    async def test_an_unmet_watch_never_rings(self):
        # The whole reason this was task-only.
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 23
        engine = _make_engine(repo=repo)

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=False, check_result={"status": "OK"})),
        ):
            with patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
                await engine._dispatch_job(self._watch_job())

        dispatch.assert_not_called()


class TestWriteNotification:
    """Writing the notification for a watch whose author left it empty.

    Moved here from agent-runner with the rest of the decision: the scheduler already has
    the check result, and a notification-only watch was otherwise paying for a whole agent
    run to have one sentence written.
    """

    @pytest.mark.asyncio
    async def test_the_model_writes_it(self):
        engine = _make_engine()
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None)
        with patch(
            "console_backend.services.scheduler_engine.gateway_chat",
            AsyncMock(return_value='  "Campaign 4821 stopped syncing."  '),
        ):
            with patch(
                "console_backend.services.scheduler_engine.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "some-model"}),
            ):
                written = await engine._write_notification(
                    job, WatchOutcome(condition_met=True, check_result={"status": "FAILED"})
                )
        # Quotes and padding stripped: this goes straight to a person.
        assert written == "Campaign 4821 stopped syncing."

    @pytest.mark.asyncio
    async def test_an_unreachable_model_still_says_something(self):
        # A watch that triggered has something to report; silence would be the worst
        # possible outcome, so the raw result is reported instead.
        engine = _make_engine()
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None)
        with patch(
            "console_backend.services.scheduler_engine.gateway_chat",
            AsyncMock(side_effect=RuntimeError("gateway down")),
        ):
            with patch(
                "console_backend.services.scheduler_engine.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "m"}),
            ):
                written = await engine._write_notification(
                    job, WatchOutcome(condition_met=True, check_result={"status": "FAILED"})
                )
        assert "triggered" in written
        assert "FAILED" in written

    @pytest.mark.asyncio
    async def test_no_configured_model_still_says_something(self):
        engine = _make_engine()
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None)
        with patch(
            "console_backend.services.scheduler_engine.ModelDefaultsRepository.get_all",
            AsyncMock(return_value={}),
        ):
            written = await engine._write_notification(
                job, WatchOutcome(condition_met=True, check_result={"status": "FAILED"})
            )
        assert "FAILED" in written

    @pytest.mark.asyncio
    async def test_an_empty_result_needs_no_model(self):
        engine = _make_engine()
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None)
        with patch(
            "console_backend.services.scheduler_engine.gateway_chat", AsyncMock()
        ) as chat:
            written = await engine._write_notification(job, WatchOutcome(condition_met=True))
        chat.assert_not_awaited()
        assert job.name in written


class TestConditionEvaluationIsPersisted:
    """The run records how its condition was decided, on every path."""

    @staticmethod
    def _watch_job() -> ScheduledJob:
        job = _make_job(job_type=JobType.WATCH, sub_agent_id=None)
        return job.model_copy(update={"check_tool": "t", "cel_expr": "result.status"})

    @pytest.mark.asyncio
    async def test_an_unmet_condition_still_records_why(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 30
        engine = _make_engine(repo=repo)
        evaluation = {"met": False, "mode": "judge", "reasoning": "nobody external was invited"}

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(
                return_value=WatchOutcome(
                    condition_met=False, check_result={"a": 1}, evaluation=evaluation
                )
            ),
        ):
            with patch("console_backend.services.scheduler_engine.dispatch_streaming"):
                await engine._dispatch_job(self._watch_job())

        assert repo.complete_run.call_args[1]["condition_evaluation"] == evaluation

    @pytest.mark.asyncio
    async def test_a_triggered_condition_records_it_too(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 31
        engine = _make_engine(repo=repo)
        evaluation = {"met": True, "mode": "judge", "reasoning": "two external attendees"}

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(
                return_value=WatchOutcome(
                    condition_met=True, check_result={"a": 1}, evaluation=evaluation
                )
            ),
        ):
            with patch(
                "console_backend.services.scheduler_engine.dispatch_streaming",
                AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
            ):
                await engine._dispatch_job(self._watch_job())

        assert repo.complete_run.call_args[1]["condition_evaluation"] == evaluation

    @pytest.mark.asyncio
    async def test_a_failed_check_records_whatever_was_decided(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 32
        engine = _make_engine(repo=repo)

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=False, error="gateway down")),
        ):
            with patch("console_backend.services.scheduler_engine.dispatch_streaming"):
                await engine._dispatch_job(self._watch_job())

        # Nothing was decided, so there is nothing to explain.
        assert repo.complete_run.call_args[1]["condition_evaluation"] is None

    @pytest.mark.asyncio
    async def test_a_task_run_records_none(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 33
        engine = _make_engine(repo=repo)

        with patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
        ):
            await engine._dispatch_job(_make_job(job_type=JobType.TASK, sub_agent_id=42))

        assert repo.complete_run.call_args[1]["condition_evaluation"] is None
