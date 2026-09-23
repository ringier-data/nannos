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
import unittest.mock
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from console_backend.models.notification import NotificationType
from console_backend.models.scheduled_job import (
    JobRunStatus,
    JobType,
    RunTrigger,
    ScheduledJob,
    ScheduleKind,
    ScheduledJobRun,
)
from console_backend.repositories.delivery_channel_repository import DeliveryChannelRepository
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from ringier_a2a_sdk.cost_tracking.attribution import current_attribution

from console_backend.services.watch_evaluator import WatchOutcome
from console_backend.services.scheduler_engine import NOTIFY_TIMEOUT_SECONDS, SchedulerEngine
from console_backend.services.scheduler_token_service import SchedulerTokenService
from console_backend.utils.a2a_dispatch import AgentUnreachable
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.scheduler_helpers import job_retry_at, make_job, run_status, seed_job, seed_run


def _make_engine(
    *,
    repo: Any = None,
    token_service: Any = None,
    db_session_factory: Any = None,
    socket_manager: Any = None,
) -> SchedulerEngine:
    repo = repo or AsyncMock(spec=ScheduledJobRepository)
    # complete_job reports the job state it left behind, so _finalize can tell "this run
    # stopped the job" from "it was already off" and notify the owner. A bare AsyncMock
    # returns something that will not unpack; the default here is "still enabled, not
    # paused", which is what nearly every test means.
    if isinstance(repo.complete_job, unittest.mock.NonCallableMock | unittest.mock.Mock) and not isinstance(
        repo.complete_job.return_value, tuple
    ):
        repo.complete_job.return_value = (True, None)
    token_service = token_service or AsyncMock(spec=SchedulerTokenService)
    delivery_channel_repo = AsyncMock(spec=DeliveryChannelRepository)
    delivery_channel_repo.get_channel_for_dispatch.return_value = None
    if db_session_factory is None:
        db_session_factory = _make_mock_session_factory()
    return SchedulerEngine(
        # Allow-all, explicitly: the check is required so a production site cannot
        # forget it; tests that exercise it replace this attribute.
        agent_access_check=AsyncMock(return_value=True),
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
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.FAILED
        assert "A2A request error: Internal error" in (outcome.error_message or "")
        assert outcome.result_summary is None

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
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.SUCCESS
        assert outcome.result_summary == "Daily report generated."
        assert outcome.error_message is None
        assert outcome.conversation_id == "ctx-123"

    def test_a2a_task_format_condition_not_met(self):
        """A2A Task artifact with condition_not_met → CONDITION_NOT_MET."""
        meta = {"scheduler_status": "condition_not_met", "agent_message": None}
        data = {
            "result": {
                "kind": "task",
                "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}],
            }
        }
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.CONDITION_NOT_MET

    def test_a2a_task_format_failed(self):
        """A2A Task artifact with scheduler_status=failed → FAILED."""
        meta = {"scheduler_status": "failed", "error_message": "Tool error"}
        data = {
            "result": {
                "kind": "task",
                "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}],
            }
        }
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.FAILED
        assert outcome.error_message == "Tool error"

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
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.SUCCESS
        assert outcome.result_summary == "Done!"

    def test_missing_scheduler_status_defaults_to_success(self):
        """When scheduler_status is absent, defaults to success."""
        data = {"result": {"metadata": {"agent_message": "something happened"}}}
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.SUCCESS

    def test_unknown_status_string_defaults_to_success(self):
        """Unrecognised scheduler_status string falls back to SUCCESS."""
        data = {"result": {"metadata": {"scheduler_status": "unknown_status_xyz"}}}
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.SUCCESS

    def test_task_state_failed_fallback(self):
        """When artifact has no scheduler_status, task.status.state=failed → FAILED."""
        data = {
            "result": {
                "kind": "task",
                "status": {"state": "failed"},
                "artifacts": [],
            }
        }
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.FAILED

    def test_a_discovery_failure_is_recorded_interrupted(self):
        """The contract with agent-runner for a gateway outage.

        agent-runner reports ``interrupted`` when it could not list the tool catalogue
        after retries. It has to survive the trip: INTERRUPTED leaves
        ``consecutive_failures`` alone in both directions and earns one fresh attempt,
        whereas FAILED would march an otherwise healthy job toward auto-pause every time
        the gateway blinked.
        """
        data = {
            "result": {
                "kind": "task",
                "status": {"state": "failed"},
                "artifacts": [
                    {
                        "parts": [
                            {
                                "kind": "text",
                                "text": json.dumps(
                                    {
                                        "scheduler_status": "interrupted",
                                        "error_message": "could not list the tool catalogue: 503",
                                    }
                                ),
                            }
                        ]
                    }
                ],
            }
        }
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.INTERRUPTED
        assert "503" in outcome.error_message

    def test_an_unparseable_park_is_not_recorded_green(self):
        """The dangerous input is the status text that does NOT parse as JSON.

        A park whose payload is prose, truncated, or written by an older runner has no
        ``scheduler_status`` at all. Defaulting every non-failed task state to success
        recorded it as a plain SUCCESS: ``consecutive_failures`` reset, the ask dropped,
        and the run green — the exact outcome ADR-0009 exists to abolish, on the one path
        where nothing is left to resume it with. It is a failure, which at least counts.
        """
        data = {
            "result": {
                "kind": "task",
                "status": {"state": "auth_required"},
                "artifacts": [{"parts": [{"kind": "text", "text": "I need you to log in first."}]}],
            }
        }
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.FAILED
        assert outcome.parked_task_id is None

    def test_a_park_that_names_its_task_is_still_a_park(self):
        """The guard above must not swallow the good case."""
        import json as _json

        data = {
            "result": {
                "kind": "task",
                "status": {"state": "auth_required"},
                "artifacts": [
                    {
                        "parts": [
                            {
                                "kind": "text",
                                "text": _json.dumps(
                                    {
                                        "scheduler_status": "auth_required",
                                        "parked_task_id": "task-abc",
                                        "agent_message": "Authorize GitHub to continue.",
                                    }
                                ),
                            }
                        ]
                    }
                ],
            }
        }
        outcome = self.engine._parse_result(data)

        assert outcome.status == JobRunStatus.AUTH_REQUIRED
        assert outcome.parked_task_id == "task-abc"


def _pg_engine(pg_session: AsyncSession) -> SchedulerEngine:
    """An engine on a real repository: the sweep is SQL, and a mocked repo would assert nothing."""
    return _make_engine(repo=ScheduledJobRepository(), db_session_factory=make_pg_session_factory(pg_session))


class TestHealStuckRuns:
    """Tests for SchedulerEngine._heal_stuck_runs() using pg_session."""

    @pytest.mark.asyncio
    async def test_sweeps_on_staleness_not_age(self, pg_session: AsyncSession):
        """A run is swept when its heartbeat goes stale — not merely because it is old."""
        job_id = await seed_job(pg_session, "heal-stale")
        # Dispatcher stopped reporting an hour ago.
        stale_run_id = await seed_run(
            pg_session, job_id, started_at="NOW() - INTERVAL '2 hours'", last_seen_at="NOW() - INTERVAL '1 hour'"
        )
        # Running for two hours and still reporting: a slow job, not an abandoned one.
        # Under the old age-based bound this run was swept while the agent worked.
        long_lived_run_id = await seed_run(
            pg_session, job_id, started_at="NOW() - INTERVAL '2 hours'", last_seen_at="NOW() - INTERVAL '5 seconds'"
        )
        await pg_session.commit()

        await _pg_engine(pg_session)._heal_stuck_runs()

        assert await run_status(pg_session, stale_run_id) == "interrupted"
        assert await run_status(pg_session, long_lived_run_id) == "running"
        # The interruption owes the job a fresh attempt.
        assert await job_retry_at(pg_session, job_id) is not None

    @pytest.mark.asyncio
    async def test_a_heartbeatless_run_keeps_the_old_age_bound(self, pg_session: AsyncSession):
        """A row with no heartbeat was written by the previous release, whose process may
        still be executing it during a rolling deploy. It is judged by age, generously."""
        job_id = await seed_job(pg_session, "heal-legacy")
        recent_legacy = await seed_run(pg_session, job_id, started_at="NOW() - INTERVAL '5 minutes'", last_seen_at=None)
        old_legacy = await seed_run(pg_session, job_id, started_at="NOW() - INTERVAL '2 hours'", last_seen_at=None)
        await pg_session.commit()

        await _pg_engine(pg_session)._heal_stuck_runs()

        assert await run_status(pg_session, recent_legacy) == "running", "60s stale would have swept a live run"
        assert await run_status(pg_session, old_legacy) == "interrupted"

    @pytest.mark.asyncio
    async def test_in_flight_runs_are_never_swept(self, pg_session: AsyncSession):
        """A run this process is dispatching is excluded regardless of its heartbeat."""
        job_id = await seed_job(pg_session, "heal-inflight")
        run_id = await seed_run(
            pg_session, job_id, started_at="NOW() - INTERVAL '2 hours'", last_seen_at="NOW() - INTERVAL '1 hour'"
        )
        await pg_session.commit()

        engine = _pg_engine(pg_session)
        engine._in_flight.add(run_id)
        await engine._heal_stuck_runs()

        assert await run_status(pg_session, run_id) == "running"

    @pytest.mark.asyncio
    async def test_interrupted_retry_earns_no_second_retry(self, pg_session: AsyncSession):
        """An interruption buys one fresh attempt; a retry that is interrupted buys none,
        and owes the user a notice instead."""
        job_id = await seed_job(pg_session, "heal-retry")
        retry_run_id = await seed_run(
            pg_session,
            job_id,
            started_at="NOW() - INTERVAL '2 hours'",
            last_seen_at="NOW() - INTERVAL '1 hour'",
            trigger="retry",
        )
        await pg_session.commit()

        await _pg_engine(pg_session)._heal_stuck_runs()

        assert await run_status(pg_session, retry_run_id) == "interrupted"
        assert await job_retry_at(pg_session, job_id) is None
        r = await pg_session.execute(
            text("SELECT notice_due_at FROM scheduled_job_runs WHERE id = :id"), {"id": retry_run_id}
        )
        assert r.scalar_one() is not None

    @pytest.mark.asyncio
    async def test_interrupted_manual_run_earns_nothing(self, pg_session: AsyncSession):
        """The user was present for a run-now and can press again; reviving it through the
        claim path would turn a test press into a scheduled execution."""
        job_id = await seed_job(pg_session, "heal-manual")
        manual_run_id = await seed_run(
            pg_session,
            job_id,
            started_at="NOW() - INTERVAL '2 hours'",
            last_seen_at="NOW() - INTERVAL '1 hour'",
            trigger="manual",
        )
        await pg_session.commit()

        await _pg_engine(pg_session)._heal_stuck_runs()

        assert await run_status(pg_session, manual_run_id) == "interrupted"
        assert await job_retry_at(pg_session, job_id) is None
        r = await pg_session.execute(
            text("SELECT notice_due_at FROM scheduled_job_runs WHERE id = :id"), {"id": manual_run_id}
        )
        assert r.scalar_one() is None

    @pytest.mark.asyncio
    async def test_heal_does_not_touch_completed_runs(self, pg_session: AsyncSession):
        """Completed runs are not affected by healing."""
        job_id = await seed_job(pg_session, "heal-done")
        old_success_run_id = await seed_run(
            pg_session, job_id, status="success", started_at="NOW() - INTERVAL '45 minutes'", completed_at="NOW()"
        )
        await pg_session.commit()

        await _pg_engine(pg_session)._heal_stuck_runs()

        assert await run_status(pg_session, old_success_run_id) == "success"

    @pytest.mark.asyncio
    async def test_a_swept_run_stays_swept_when_its_dispatcher_finishes(self, pg_session: AsyncSession):
        """If the healer was wrong and the dispatcher was merely slow to report, its late
        completion must not flip the row back: the retry is already on its way, and a
        row reading 'success' would hide that the job ran twice."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "heal-late")
        run_id = await seed_run(
            pg_session, job_id, started_at="NOW() - INTERVAL '10 minutes'", last_seen_at="NOW() - INTERVAL '5 minutes'"
        )
        await pg_session.commit()

        await _pg_engine(pg_session)._heal_stuck_runs()
        assert await run_status(pg_session, run_id) == "interrupted"

        changed = await repo.complete_run(pg_session, run_id, JobRunStatus.SUCCESS, delivered=True)
        await pg_session.commit()

        assert changed is False
        assert await run_status(pg_session, run_id) == "interrupted"


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
        repo.complete_job = AsyncMock(return_value=(True, None))
        repo.complete_run = AsyncMock(side_effect=RuntimeError("column does not exist"))

        engine = _make_engine(repo=repo)
        job = make_job(job_id=77, schedule_kind=ScheduleKind.INTERVAL, interval_seconds=3600)

        # Must not raise: the caller has nothing useful to do with a bookkeeping failure.
        await engine._finalize(run_id=99, job=job, status=JobRunStatus.SUCCESS)

        repo.complete_job.assert_awaited_once()
        kwargs = repo.complete_job.call_args[1]
        assert kwargs["next_run_at"] is not None
        assert kwargs["next_run_at"] > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_a_run_that_cannot_be_recorded_is_still_closed(self):
        """Left 'running', the healer would call it interrupted and re-execute a job whose
        result the user already has. The fallback touches only columns the table has
        always had — the write that shaped this code failed on a column it did not."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_job = AsyncMock(return_value=(True, None))
        repo.complete_run = AsyncMock(side_effect=RuntimeError("column does not exist"))
        repo.close_run_minimally = AsyncMock()

        engine = _make_engine(repo=repo)
        await engine._finalize(run_id=99, job=make_job(), status=JobRunStatus.SUCCESS)

        repo.close_run_minimally.assert_awaited_once()
        assert repo.close_run_minimally.call_args[0][1:3] == (99, JobRunStatus.SUCCESS)

    @pytest.mark.asyncio
    async def test_schedule_advances_before_the_run_is_recorded(self):
        """Ordering, not just independence — the advance cannot be the write's hostage."""
        calls: list[str] = []
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_job = AsyncMock(side_effect=lambda **_: (calls.append("job"), (True, None))[1])
        repo.complete_run = AsyncMock(side_effect=lambda **_: calls.append("run"))

        engine = _make_engine(repo=repo)
        await engine._finalize(run_id=1, job=make_job(), status=JobRunStatus.SUCCESS)

        assert calls == ["job", "run"]


class TestDispatchJobNoToken:
    """When _token_service raises ValueError (no offline token), job is auto-paused."""

    @pytest.mark.asyncio
    async def test_auto_pauses_when_no_offline_token(self):
        """dispatch_job() auto-pauses the job when SchedulerTokenService raises ValueError."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 1
        repo.complete_job = AsyncMock(return_value=(True, None))
        repo.complete_run = AsyncMock()

        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.side_effect = ValueError("No offline token stored")

        engine = _make_engine(repo=repo, token_service=token_service)
        job = make_job()

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
        repo.complete_job = AsyncMock(return_value=(True, None))

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
        assert kwargs["status"] is JobRunStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_failure_increments_passed_to_repo(self):
        """On failure the status reaches the repo, which is what increments consecutive_failures."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock(return_value=(True, None))

        engine = _make_engine(repo=repo)
        interval_job = make_job(schedule_kind=ScheduleKind.INTERVAL, interval_seconds=300)

        await engine._finalize(run_id=5, job=interval_job, status=JobRunStatus.FAILED, error_message="Oops")

        kwargs = repo.complete_job.call_args[1]
        assert kwargs["status"] is JobRunStatus.FAILED
        # A failure is not an interruption: no fresh attempt is owed.
        assert kwargs["retry_at"] is None

    @pytest.mark.asyncio
    async def test_condition_not_met_counts_as_success(self):
        """CONDITION_NOT_MET is treated as success (no failure increment)."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock(return_value=(True, None))

        engine = _make_engine(repo=repo)
        watch_job = make_job(job_type=JobType.WATCH, schedule_kind=ScheduleKind.INTERVAL, interval_seconds=60)

        await engine._finalize(run_id=2, job=watch_job, status=JobRunStatus.CONDITION_NOT_MET)

        kwargs = repo.complete_job.call_args[1]
        assert kwargs["status"] is JobRunStatus.CONDITION_NOT_MET

    @pytest.mark.asyncio
    async def test_destroy_after_trigger_disables_watch_job(self):
        """Watch job with destroy_after_trigger=True is disabled via SQL after SUCCESS."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock(return_value=(True, None))

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.execute = AsyncMock()

        @asynccontextmanager
        async def factory():
            yield mock_db

        engine = _make_engine(repo=repo, db_session_factory=factory)

        watch_job = make_job(
            job_type=JobType.WATCH,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=60,
            destroy_after_trigger=True,
        )
        await engine._finalize(run_id=3, job=watch_job, status=JobRunStatus.SUCCESS)

        # The subscription that fired is disabled — this subscriber's, nobody else's.
        repo.disable_subscription.assert_awaited_once()
        assert repo.disable_subscription.call_args.args[1] == watch_job.id

    @pytest.mark.asyncio
    async def test_watch_job_without_destroy_after_trigger_stays_enabled(self):
        """Watch job with destroy_after_trigger=False stays enabled after SUCCESS."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock(return_value=(True, None))

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.execute = AsyncMock()

        @asynccontextmanager
        async def factory():
            yield mock_db

        engine = _make_engine(repo=repo, db_session_factory=factory)

        watch_job = make_job(
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
        repo.complete_job = AsyncMock(return_value=(True, None))

        engine = _make_engine(repo=repo)
        interval_job = make_job()

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
        repo.complete_job = AsyncMock(return_value=(True, None))

        socket_manager = AsyncMock()
        socket_manager.send_notification = AsyncMock(return_value=True)

        engine = _make_engine(repo=repo, socket_manager=socket_manager)
        job = make_job(user_id="notify-user")

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
        repo.complete_job = AsyncMock(return_value=(True, None))
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
            await engine._dispatch_job(make_job(), run_id=99)

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
        repo.complete_job = AsyncMock(return_value=(True, None))
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token = AsyncMock(return_value="token-xyz")

        engine = _make_engine(repo=repo, token_service=token_service)

        with patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            new=AsyncMock(side_effect=RuntimeError("assessor exploded")),
        ):
            await engine._dispatch_job(make_job(), run_id=99)

        repo.complete_run.assert_awaited_once()
        kwargs = repo.complete_run.await_args.kwargs
        assert kwargs["status"] == JobRunStatus.FAILED
        assert "assessor exploded" in (kwargs["error_message"] or "")


class TestInterruptionIsDecidedByTheDispatch:
    """Only dispatch_streaming can say the agent died. It says so with AgentUnreachable;
    everything else that goes wrong on the way to it is a failure of the run."""

    @staticmethod
    def _engine(token_error: Exception | None = None) -> tuple[SchedulerEngine, AsyncMock]:
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run = AsyncMock(return_value=99)
        token_service = AsyncMock(spec=SchedulerTokenService)
        if token_error is None:
            token_service.get_access_token = AsyncMock(return_value="token-xyz")
        else:
            token_service.get_access_token = AsyncMock(side_effect=token_error)
        return _make_engine(repo=repo, token_service=token_service), repo

    @pytest.mark.asyncio
    async def test_an_unreachable_runner_interrupts_a_scheduled_run_and_earns_a_retry(self):
        engine, repo = self._engine()
        with patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            new=AsyncMock(side_effect=AgentUnreachable("connection refused")),
        ):
            await engine._dispatch_job(make_job(), run_id=99)

        assert repo.complete_run.await_args.kwargs["status"] == JobRunStatus.INTERRUPTED
        assert repo.complete_run.await_args.kwargs["notice_due_at"] is None, "the first loss is not news"
        assert repo.complete_job.await_args.kwargs["retry_at"] is not None

    @pytest.mark.asyncio
    async def test_an_interrupted_retry_owes_a_notice_and_no_further_retry(self):
        engine, repo = self._engine()
        with patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            new=AsyncMock(side_effect=AgentUnreachable("connection refused")),
        ):
            await engine._dispatch_job(make_job(), run_id=99, trigger=RunTrigger.RETRY)

        assert repo.complete_run.await_args.kwargs["status"] == JobRunStatus.INTERRUPTED
        assert repo.complete_run.await_args.kwargs["notice_due_at"] is not None
        assert repo.complete_job.await_args.kwargs["retry_at"] is None

    @pytest.mark.asyncio
    async def test_an_interrupted_manual_run_earns_neither(self):
        """The user pressed Run Now and is present; a retry would turn a test press into a
        scheduled execution, and a notice would tell them what they watched happen."""
        engine, repo = self._engine()
        with patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            new=AsyncMock(side_effect=AgentUnreachable("connection refused")),
        ):
            await engine.run_job_now(make_job(), run_id=99)

        assert repo.complete_run.await_args.kwargs["status"] == JobRunStatus.INTERRUPTED
        assert repo.complete_run.await_args.kwargs["notice_due_at"] is None
        assert repo.complete_job.await_args.kwargs["retry_at"] is None

    @pytest.mark.asyncio
    async def test_a_transport_error_before_the_dispatch_is_a_failure(self):
        """Keycloak refusing the token refresh is not the agent dying: nothing was
        running, so it counts against the job like any other failure."""
        engine, repo = self._engine(
            token_error=httpx.ConnectError("keycloak down", request=httpx.Request("POST", "http://keycloak/"))
        )
        with patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
            await engine._dispatch_job(make_job(), run_id=99)

        dispatch.assert_not_called()
        assert repo.complete_run.await_args.kwargs["status"] == JobRunStatus.FAILED
        assert repo.complete_job.await_args.kwargs["retry_at"] is None


class TestFinalizeInvalidTimezone:
    """An unresolvable stored timezone must pause the job, not crash _finalize.

    If _finalize raised instead, next_run_at would stay in the past and
    claim_due_jobs would re-claim and re-execute the job on every tick.
    """

    @pytest.mark.asyncio
    async def test_invalid_timezone_pauses_job(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.complete_run = AsyncMock()
        repo.complete_job = AsyncMock(return_value=(True, None))

        engine = _make_engine(repo=repo)
        job = make_job(schedule_kind=ScheduleKind.CRON, interval_seconds=None)
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
        job = make_job(job_type=JobType.WATCH)
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
        job = make_job(job_type=JobType.WATCH)
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
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None)
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
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None)
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
        job = make_job(delivery_channel_id=3)

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
        job = make_job(delivery_channel_id=None)

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
        job = make_job(delivery_channel_id=3).model_copy(update={"voice_call": True})

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
        job = make_job(delivery_channel_id=3).model_copy(update={"voice_call": True})

        with patch.object(engine, "_resolve_voice_agent_id", AsyncMock(return_value=None)):
            _, metadata, _ = await engine._build_message_args(
                job, run_id=7, access_token="tok", db=AsyncMock()
            )

        assert metadata["messageFormatting"] == "slack"


class TestProvenanceTravelsWithTheDispatch:
    """A run of a shared job says why its subscriber is receiving it (ADR-0010).

    Carried in the dispatch metadata and appended by agent-runner, where every run's
    output is composed — one seam instead of the same footer in three delivery clients.
    """

    def test_an_unshared_job_carries_none(self):
        assert SchedulerEngine._provenance_line(make_job()) is None

    def test_a_subscribed_job_names_the_owner(self):
        line = SchedulerEngine._provenance_line(
            make_job(user_id="member", owner_user_id="boss", owner_email="boss@x.test", name="Monday report")
        )
        assert line is not None
        assert "you subscribed to 'Monday report'" in line and "boss@x.test" in line

    def test_a_group_default_says_it_was_not_their_doing(self):
        line = SchedulerEngine._provenance_line(
            make_job(
                user_id="member",
                owner_user_id="boss",
                owner_email="boss@x.test",
                activated_by="group",
            )
        )
        assert line is not None and "default job of one of your groups" in line

    def test_a_vanished_owner_still_produces_a_line(self):
        # owner_email is a LEFT join: a deleted account leaves the job runnable.
        line = SchedulerEngine._provenance_line(make_job(user_id="member", owner_user_id="ghost"))
        assert line is not None and "another user" in line

    @pytest.mark.asyncio
    async def test_the_metadata_key_is_present_and_null_when_there_is_nothing_to_say(self):
        engine = _make_engine()
        _, metadata, _ = await engine._build_message_args(
            make_job(), run_id=3, access_token="tok", db=AsyncMock()
        )
        assert metadata["scheduled_job_provenance"] is None

        _, shared_meta, _ = await engine._build_message_args(
            make_job(user_id="member", owner_user_id="boss", owner_email="boss@x.test"),
            run_id=3,
            access_token="tok",
            db=AsyncMock(),
        )
        assert "boss@x.test" in shared_meta["scheduled_job_provenance"]


class TestWatchEvaluatedBeforeDispatch:
    """A watch's condition is decided here, before anything is dispatched.

    The point of moving it: a poll that does not trigger costs no agent run, and a poll
    that does can choose its target — which is what makes a voice-call watch possible.
    """

    @staticmethod
    def _watch_job(**overrides) -> ScheduledJob:
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None, **overrides)
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


class TestACheckNeedingAuthorizationParksTheRun:
    """ADR-0009 on the path the ADR did not name: the watch check itself.

    The check runs here, before any dispatch, so a ``need-credentials`` from it never
    reached agent-runner's park. It failed the run with the raw payload as the message,
    delivered nothing, and counted toward ``max_failures``.
    """

    ASK = {
        "requires_auth": True,
        "auth_requirement": {
            "service": "",
            "resource": "naonous_get_campaign",
            "auth_methods": [{"method": "oauth2", "description": "x", "auth_url": "https://gw.example/begin"}],
            "required_scopes": [],
            "token_type": "Bearer",
        },
    }

    @staticmethod
    def _watch_job(**overrides) -> ScheduledJob:
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None, **overrides)
        return job.model_copy(update={"check_tool": "naonous_get_campaign", "cel_expr": "result.status == 'FAILED'"})

    def _parked_engine(self, run_id: int):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = run_id
        engine = _make_engine(repo=repo)
        evaluate = patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=False, auth_ask=dict(self.ASK))),
        )
        return repo, engine, evaluate

    @pytest.mark.asyncio
    async def test_the_run_parks_with_the_ask_and_holds_the_schedule(self):
        repo, engine, evaluate = self._parked_engine(run_id=21)

        with evaluate, patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
            await engine._dispatch_job(self._watch_job())

        dispatch.assert_not_called()  # no channel: nothing to tell, nowhere to tell it
        run = repo.complete_run.call_args[1]
        assert run["status"] == JobRunStatus.AUTH_REQUIRED
        assert run["parked_task_id"] == "watch-check:21", "answerable, and addressed to the check itself"
        assert run["parked_payload"] == self.ASK
        assert run["error_message"] is None
        assert "https://" not in (run["result_summary"] or ""), "the URL lives in the ask, not the row"
        # Neutral about the job: complete_job sees AUTH_REQUIRED, which leaves
        # consecutive_failures alone and never auto-pauses.
        assert repo.complete_job.call_args[1]["status"] == JobRunStatus.AUTH_REQUIRED

    @staticmethod
    def _channel(**overrides) -> dict:
        return {"webhook_url": "https://hooks.example/x", "secret": "s", "message_formatting": "slack", **overrides}

    @staticmethod
    def _published_park(run_id: int) -> dict:
        """What agent-runner answers when it published the ask as a park."""
        meta = {
            "scheduler_status": "auth_required",
            "agent_message": "prose with the link",
            "parked_task_id": "notice-task-1",
            "reply_to": {"service": "console-backend", "endpoint": "scheduled_run_resume", "scheduled_job_run_id": run_id},
        }
        return {"result": {"kind": "task", "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}]}}

    @pytest.mark.asyncio
    async def test_a_job_with_a_channel_gets_the_card_through_agent_runner(self):
        # Published as a PARK, not posted as prose: the ask rides on ``auth_ask`` and the
        # runner publishes its task as auth_required, so the clients render the card
        # whose button answers this run. The first cut sent a sentence with the link and
        # "confirm in the console"; the owner authorized and waited for nothing.
        repo, engine, evaluate = self._parked_engine(run_id=22)
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = self._channel()

        with evaluate, patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            AsyncMock(return_value=self._published_park(22)),
        ) as dispatch:
            await engine._dispatch_job(self._watch_job(delivery_channel_id=5))

        call = dispatch.await_args[1]
        assert call["metadata"]["auth_ask"] == self.ASK
        assert "sub_agent_id" not in call["metadata"], "nothing runs"
        # The run id IS on it: the card's button answers reply_to.scheduled_job_run_id.
        # Not adoptable for that — every client skips provenance for a park.
        assert call["metadata"]["scheduled_job_run_id"] == 22
        assert call["metadata"]["scheduled_job_name"] == "Test Job", "the card names the job that stopped"
        assert call["metadata"]["messageFormatting"] == "slack"
        assert "reply_to_message" not in call["metadata"], "a first ask threads under nothing"
        # The prose fallback still carries a working link for a client without the card.
        assert "https://gw.example/begin" in call["parts"][0]["text"]
        assert "Test Job" in call["parts"][0]["text"]
        assert call["push_config"] == {"url": "https://hooks.example/x", "token": "s"}
        assert call["timeout_read"] == NOTIFY_TIMEOUT_SECONDS, "a dead runner must not hold the park for minutes"
        run = repo.complete_run.call_args[1]
        assert run["status"] == JobRunStatus.AUTH_REQUIRED
        assert run["delivered"] is True
        # Still addressed to the CHECK: the notice task is never answered, the run is.
        assert run["parked_task_id"] == "watch-check:22"
        assert run["parked_payload"] == self.ASK

    @pytest.mark.asyncio
    async def test_an_older_runner_that_posts_prose_still_counts_as_told(self):
        # Image skew: a runner without ``auth_ask`` completes the task and the clients
        # post the sentence. The owner has the link and the console; not a failure.
        repo, engine, evaluate = self._parked_engine(run_id=24)
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = self._channel()
        meta = {"scheduler_status": "success", "agent_message": "prose with the link"}
        completed = {"result": {"kind": "task", "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}]}}

        with evaluate, patch(
            "console_backend.services.scheduler_engine.dispatch_streaming", AsyncMock(return_value=completed)
        ):
            await engine._dispatch_job(self._watch_job(delivery_channel_id=5))

        run = repo.complete_run.call_args[1]
        assert run["status"] == JobRunStatus.AUTH_REQUIRED
        assert run["parked_task_id"] == "watch-check:24"
        assert run["delivered"] is True

    @pytest.mark.asyncio
    async def test_a_runner_that_fails_the_notice_leaves_the_run_parked_but_undelivered(self):
        repo, engine, evaluate = self._parked_engine(run_id=25)
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = self._channel()
        meta = {"scheduler_status": "failed", "error_message": "boom"}
        failed = {"result": {"kind": "task", "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}]}}

        with evaluate, patch(
            "console_backend.services.scheduler_engine.dispatch_streaming", AsyncMock(return_value=failed)
        ):
            await engine._dispatch_job(self._watch_job(delivery_channel_id=5))

        run = repo.complete_run.call_args[1]
        assert run["status"] == JobRunStatus.AUTH_REQUIRED
        assert run["delivered"] is False

    @pytest.mark.asyncio
    async def test_a_failed_notice_still_parks_the_run(self):
        # The ask is on the run for the console either way; a stopped job it shows beats
        # a failed run counted against the job.
        repo, engine, evaluate = self._parked_engine(run_id=23)
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = {"webhook_url": "u", "secret": "s"}

        with evaluate, patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            AsyncMock(side_effect=RuntimeError("runner down")),
        ):
            await engine._dispatch_job(self._watch_job(delivery_channel_id=5))

        run = repo.complete_run.call_args[1]
        assert run["status"] == JobRunStatus.AUTH_REQUIRED
        assert run["parked_task_id"] == "watch-check:23"
        assert run["delivered"] is False

    @staticmethod
    def _parked_run(run_id: int) -> ScheduledJobRun:
        return ScheduledJobRun(
            id=run_id,
            job_id=1,
            started_at=datetime.now(timezone.utc),
            status=JobRunStatus.AUTH_REQUIRED,
            delivered=False,
            parked_task_id=f"watch-check:{run_id}",
        )

    @pytest.mark.asyncio
    async def test_approving_reruns_the_check_at_once(self):
        # The credential is at the gateway now: the check succeeds and the watch carries
        # on — here the condition holds, so the trigger dispatches like any other poll.
        repo = AsyncMock(spec=ScheduledJobRepository)
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.return_value = "token"
        engine = _make_engine(repo=repo, token_service=token_service)

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=True, check_result={"status": "FAILED"})),
        ) as evaluate, patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
        ) as dispatch:
            returned = await engine.resume_parked_run(self._watch_job(), self._parked_run(30), "approved", run_id=31)

        assert returned == 31
        evaluate.assert_awaited_once()
        dispatch.assert_awaited_once()
        assert dispatch.await_args[1].get("task_id") is None, "there is no agent-runner task to continue"
        run = repo.complete_run.call_args[1]
        assert run["run_id"] == 31
        assert run["status"] == JobRunStatus.SUCCESS
        # A resumed run does not own the schedule: the parked run already advanced it.
        assert repo.complete_job.call_args[1]["leave_schedule"] is True

    @pytest.mark.asyncio
    async def test_approving_a_quiet_check_leaves_the_schedule_alone(self):
        # The mainline approval outcome: the credential is stored, the check runs, and
        # nothing is happening. The parked run already advanced next_run_at; recomputing
        # it here would skip the catch-up occurrence and overwrite a concurrent edit.
        repo = AsyncMock(spec=ScheduledJobRepository)
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.return_value = "token"
        engine = _make_engine(repo=repo, token_service=token_service)

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=False, check_result={"status": "OK"})),
        ), patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
            await engine.resume_parked_run(self._watch_job(), self._parked_run(60), "approved", run_id=61)

        dispatch.assert_not_called()
        assert repo.complete_run.call_args[1]["status"] == JobRunStatus.CONDITION_NOT_MET
        assert repo.complete_job.call_args[1]["leave_schedule"] is True
        assert repo.complete_job.call_args[1]["next_run_at"] is None

    @pytest.mark.asyncio
    async def test_a_still_missing_credential_parks_again_on_the_new_run(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.return_value = "token"
        engine = _make_engine(repo=repo, token_service=token_service)

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=False, auth_ask=dict(self.ASK))),
        ):
            await engine.resume_parked_run(self._watch_job(), self._parked_run(40), "approved", run_id=41)

        run = repo.complete_run.call_args[1]
        assert run["status"] == JobRunStatus.AUTH_REQUIRED
        assert run["parked_task_id"] == "watch-check:41"

    @pytest.mark.asyncio
    async def test_an_answer_from_a_card_threads_the_rerun_under_it(self):
        # The card's coordinates ride into the re-run's dispatch, as the agent branch
        # carries them, so the result — or a second ask — lands under the ask (ADR-0009
        # decision 5). Here the credential is still missing: the re-park's card threads.
        repo = AsyncMock(spec=ScheduledJobRepository)
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.return_value = "token"
        engine = _make_engine(repo=repo, token_service=token_service)
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = self._channel()
        card = {"channel": "D1", "ts": "1700000000.1"}

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=False, auth_ask=dict(self.ASK))),
        ), patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            AsyncMock(return_value=self._published_park(43)),
        ) as dispatch:
            await engine.resume_parked_run(
                self._watch_job(delivery_channel_id=5), self._parked_run(42), "approved", reply_to=card, run_id=43
            )

        assert dispatch.await_args[1]["metadata"]["reply_to_message"] == card
        assert dispatch.await_args[1]["metadata"]["auth_ask"] == self.ASK
        assert repo.complete_run.call_args[1]["parked_task_id"] == "watch-check:43"

    @pytest.mark.asyncio
    async def test_an_answer_from_a_card_threads_the_triggered_result_under_it(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.return_value = "token"
        engine = _make_engine(repo=repo, token_service=token_service)
        engine._delivery_channel_repo.get_channel_for_dispatch.return_value = self._channel()
        card = {"channel": "D1", "ts": "1700000000.1"}

        with patch.object(
            engine._watch_evaluator,
            "evaluate",
            AsyncMock(return_value=WatchOutcome(condition_met=True, check_result={"status": "FAILED"})),
        ), patch(
            "console_backend.services.scheduler_engine.dispatch_streaming",
            AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
        ) as dispatch:
            await engine.resume_parked_run(
                self._watch_job(delivery_channel_id=5), self._parked_run(44), "approved", reply_to=card, run_id=45
            )

        assert dispatch.await_args[1]["metadata"]["reply_to_message"] == card
        assert "auth_ask" not in dispatch.await_args[1]["metadata"]
        assert repo.complete_run.call_args[1]["status"] == JobRunStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_declining_releases_the_schedule_without_counting_a_failure(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        engine = _make_engine(repo=repo)

        with patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
            await engine.resume_parked_run(self._watch_job(), self._parked_run(50), "declined", run_id=51)

        dispatch.assert_not_called()
        run = repo.complete_run.call_args[1]
        assert run["status"] == JobRunStatus.FAILED
        assert "declined" in run["error_message"]
        # counts_as_failure=False: the job sees a neutral status, not a failure.
        assert repo.complete_job.call_args[1]["status"] == JobRunStatus.INTERRUPTED
        assert repo.complete_job.call_args[1]["leave_schedule"] is True
        repo.disable_subscription.assert_not_called()


class TestVoiceCallDispatch:
    """A voice call is a delivery choice, not a job-type capability.

    It was task-only while agent-runner evaluated watch conditions: dispatch preceded the
    verdict, so a voice watch would have rung on every poll. The scheduler decides first
    now, so a call only happens because something happened.
    """

    @staticmethod
    def _watch_job(**overrides) -> ScheduledJob:
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None, **overrides)
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
        job = make_job(job_type=JobType.TASK, sub_agent_id=42).model_copy(update={"voice_call": True})
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


def _make_job(**overrides: Any) -> ScheduledJob:
    """A minimal enabled task job.

    ``TestAttributionScope`` has always called this and it was never defined, so those
    three tests have never run. Added here rather than left red: the attribution scope
    they cover is what keeps a scheduled run's spend attributed to the job instead of
    reading as the owner's own chat usage.
    """
    defaults: dict[str, Any] = {
        "id": 1,
        "user_id": "user-1",
        "name": "Test job",
        "job_type": JobType.TASK,
        "schedule_kind": ScheduleKind.INTERVAL,
        "interval_seconds": 3600,
        "timezone": "UTC",
        "enabled": True,
        "next_run_at": datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
        "consecutive_failures": 0,
        "max_failures": 3,
        "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "delivered": False,
    }
    return ScheduledJob(**{**defaults, **overrides})


class TestAttributionScope:
    """Every gateway call a dispatch makes bills to the job's owner and the job.

    Both LLM calls on this path (the watch judge, the notification writer) used to run
    inside the agent, where the SDK's attribution ContextVars carried the owner and the
    job id for free. Moving the decision into the scheduler (#166) took them out of that
    context and nothing replaced it, so the proxy's logger dropped their records and the
    spend left `usage_logs` entirely. The scope puts them back in it — and covers whatever
    gateway call this path grows next, without a parameter threaded to it.
    """

    @pytest.mark.asyncio
    async def test_the_dispatch_runs_as_the_job_s_owner(self):
        seen: dict = {}
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 11
        engine = _make_engine(repo=repo)
        job = _make_job()

        async def _snapshot(*args, **kwargs):
            # What a gateway call made anywhere under the dispatch would stamp.
            seen.update(current_attribution())
            return [], {}, None

        with patch("console_backend.services.scheduler_engine.billing_subject", AsyncMock(return_value="oidc-subject")):
            with patch.object(engine, "_build_message_args", _snapshot):
                with patch(
                    "console_backend.services.scheduler_engine.dispatch_streaming",
                    AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
                ):
                    await engine._dispatch_job(job)

        # scheduled_job_id is not decoration: usage_repository derives the service
        # dimension from it, so without it this recurring overhead is booked as the
        # user's own 'orchestrator' spend.
        assert seen == {"user_sub": "oidc-subject", "scheduled_job_id": job.id, "service": "scheduler"}

    @pytest.mark.asyncio
    async def test_the_scope_does_not_outlive_the_dispatch(self):
        """`run_job_now` awaits the dispatch inline from a request handler — a scope left
        open there would bill the rest of that request to the job."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 12
        engine = _make_engine(repo=repo)

        with patch("console_backend.services.scheduler_engine.billing_subject", AsyncMock(return_value="oidc-subject")):
            with patch.object(engine, "_build_message_args", AsyncMock(return_value=([], {}, None))):
                with patch(
                    "console_backend.services.scheduler_engine.dispatch_streaming",
                    AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
                ):
                    await engine.run_job_now(_make_job())

        assert current_attribution() == {}

    @pytest.mark.asyncio
    async def test_an_unresolvable_owner_still_dispatches(self):
        """Worse accounting, not a dead job — and the internal id still bills the right
        person, because the ingest resolves either. Dropping to no subject at all would
        lose the usage row entirely, which is the failure this whole path is about."""
        seen: dict = {}
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 13
        engine = _make_engine(repo=repo)
        job = _make_job()

        async def _snapshot(*args, **kwargs):
            seen.update(current_attribution())
            return [], {}, None

        # What `billing_subject` returns when the subject cannot be read.
        with patch(
            "console_backend.services.scheduler_engine.billing_subject",
            AsyncMock(return_value=job.user_id),
        ):
            with patch.object(engine, "_build_message_args", _snapshot):
                with patch(
                    "console_backend.services.scheduler_engine.dispatch_streaming",
                    AsyncMock(return_value={"result": {"kind": "task", "artifacts": []}}),
                ) as dispatch:
                    await engine._dispatch_job(job)

        dispatch.assert_awaited_once()
        assert seen == {"user_sub": job.user_id, "scheduled_job_id": job.id, "service": "scheduler"}


class TestWriteNotification:
    """Writing the notification for a watch whose author left it empty.

    Moved here from agent-runner with the rest of the decision: the scheduler already has
    the check result, and a notification-only watch was otherwise paying for a whole agent
    run to have one sentence written.
    """

    @pytest.mark.asyncio
    async def test_the_model_writes_it(self):
        engine = _make_engine()
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None)
        chat = AsyncMock(return_value='  "Campaign 4821 stopped syncing."  ')
        with patch("console_backend.services.scheduler_engine.gateway_chat", chat):
            with patch(
                "console_backend.services.scheduler_engine.ModelDefaultsRepository.get_all",
                AsyncMock(return_value={"chat:low": "some-model"}),
            ):
                written = await engine._write_notification(
                    job, WatchOutcome(condition_met=True, check_result={"status": "FAILED"})
                )
        # Quotes and padding stripped: this goes straight to a person.
        assert written == "Campaign 4821 stopped syncing."
        # Thinking off: a reasoning model on the low tier would otherwise spend the
        # 256-token budget thinking and send a cut-off sentence to the person.
        assert chat.await_args.kwargs["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    async def test_an_unreachable_model_still_says_something(self):
        # A watch that triggered has something to report; silence would be the worst
        # possible outcome, so the raw result is reported instead.
        engine = _make_engine()
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None)
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
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None)
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
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None)
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
        job = make_job(job_type=JobType.WATCH, sub_agent_id=None)
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
            await engine._dispatch_job(make_job(job_type=JobType.TASK, sub_agent_id=42))

        assert repo.complete_run.call_args[1]["condition_evaluation"] is None


class TestParkedRuns:
    """A run blocked on the owner's credential parks; it neither fails nor succeeds.

    See docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.
    """

    def setup_method(self):
        self.engine = _make_engine()

    @staticmethod
    def _result(meta: dict) -> dict:
        return {
            "result": {
                "kind": "task",
                "contextId": "ctx-parked",
                "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(meta)}]}],
            }
        }

    def test_a_parked_payload_carries_the_task_and_the_ask_home(self):
        ask = {"requires_auth": True, "auth_requirement": {"service": "github", "auth_methods": []}}
        outcome = self.engine._parse_result(
            self._result(
                {
                    "scheduler_status": "auth_required",
                    "agent_message": "I need access to GitHub.",
                    "parked_task_id": "outer-task-1",
                    "auth_payload": ask,
                }
            )
        )
        assert outcome.status == JobRunStatus.AUTH_REQUIRED
        # Both are needed to reach the run again: one to address the task, one to put
        # the question to its owner.
        assert outcome.parked_task_id == "outer-task-1"
        assert outcome.parked_payload == ask

    def test_a_park_with_no_task_to_address_is_a_failure_not_a_stopped_job(self):
        """Recording it as parked would stop the job with nothing able to restart it.

        The schedule hold means a parked run blocks every later occurrence, so a park
        nobody can answer is a job silently retired — worse than a failure, which at
        least shows up and eventually pauses the job with a reason.
        """
        outcome = self.engine._parse_result(
            self._result({"scheduler_status": "auth_required", "agent_message": "blocked"})
        )
        assert outcome.status == JobRunStatus.FAILED
        assert "nothing to resume" in (outcome.error_message or "")

    def test_an_ordinary_run_carries_no_parked_state(self):
        outcome = self.engine._parse_result(
            self._result({"scheduler_status": "success", "agent_message": "Done."})
        )
        assert outcome.status == JobRunStatus.SUCCESS
        assert outcome.parked_task_id is None
        assert outcome.parked_payload is None


class TestAutoPauseIsNotSilent:
    """A job that stops itself has to say so.

    Auto-pause is decided inside ``complete_job`` from consecutive_failures against
    max_failures, and it used to happen in silence: the job simply stopped producing,
    which is the one symptom its owner is least likely to notice. The delivery channel
    cannot carry the notice — it is optional, and a job without one is exactly the job
    whose silence goes unnoticed — so it is a durable console notification.
    """

    @staticmethod
    def _job(enabled: bool = True) -> ScheduledJob:
        return ScheduledJob(
            id=7,
            user_id="user-1",
            name="QA GitHub Identity Check",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=3600,
            timezone="UTC",
            enabled=enabled,
            next_run_at=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
            consecutive_failures=2,
            max_failures=3,
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            delivered=False,
        )

    async def _finalize(self, repo_result, job):
        engine = _make_engine()
        engine._repo.complete_job = AsyncMock(return_value=repo_result)
        engine._repo.complete_run = AsyncMock(return_value=True)
        engine._notification_service = AsyncMock()
        await engine._finalize(run_id=99, job=job, status=JobRunStatus.FAILED, error_message="boom")
        return engine._notification_service

    @pytest.mark.asyncio
    async def test_the_owner_is_told_when_a_run_stops_their_job(self):
        notifications = await self._finalize((False, "Auto-paused after 3 consecutive failures"), self._job())
        notifications.create_notification.assert_awaited_once()
        kwargs = notifications.create_notification.await_args.kwargs
        assert kwargs["user_id"] == "user-1"
        assert kwargs["notification_type"] == NotificationType.SCHEDULED_JOB_PAUSED
        # The reason the job carries, not a generic line: it is what tells the owner
        # whether to fix the job or just resume it.
        assert "3 consecutive failures" in kwargs["message"]
        assert kwargs["metadata"] == {"job_id": 7, "run_id": 99}

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_that_leaves_the_job_running_says_nothing(self):
        """One failed run is not news; the run history already records it."""
        notifications = await self._finalize((True, None), self._job())
        notifications.create_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_job_that_was_already_paused_is_not_announced_again(self):
        notifications = await self._finalize((False, "Auto-paused after 3 consecutive failures"), self._job(enabled=False))
        notifications.create_notification.assert_not_awaited()


class TestAnAskIsAnsweredOnce:
    """Claiming the ask is what makes a second click harmless.

    The parked run KEEPS its ``auth_required`` status after being answered — that is a
    true record of how the occurrence ended — so the status cannot say whether a question
    is still outstanding. ``parked_task_id`` does. Without clearing it the console went on
    offering the card after the answer, and the second press reached a task that had since
    gone terminal, surfacing the A2A server's raw "task is in terminal state" at the user.
    """

    @pytest.mark.asyncio
    async def test_the_claim_is_conditional_so_only_one_click_wins(self):
        repo = ScheduledJobRepository()
        db = AsyncMock()
        # Second caller: the row no longer has a task id, so nothing is updated.
        db.execute = AsyncMock(return_value=MagicMock(rowcount=0))
        assert await repo.clear_parked_task(db, 616) is False

        db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
        assert await repo.clear_parked_task(db, 616) is True

    @pytest.mark.asyncio
    async def test_a_run_that_is_no_longer_waiting_is_refused_not_resumed(self):
        engine = _make_engine()
        answered = ScheduledJobRun(
            id=616,
            job_id=7,
            started_at=datetime(2026, 9, 15, 21, 33, tzinfo=timezone.utc),
            status=JobRunStatus.AUTH_REQUIRED,
            delivered=True,
            parked_task_id=None,  # claimed by whoever answered first
        )
        # Logged and refused, not raised: this body runs as a Starlette background task,
        # where an exception escapes into nothing at all. Nothing is dispatched and no run
        # row is opened for an answer there was no question for.
        engine._repo.create_run = AsyncMock()
        result = await engine.resume_parked_run(TestAutoPauseIsNotSilent._job(), answered, "approved")
        assert result == 616
        engine._repo.create_run.assert_not_awaited()


class TestOnlyOneRunIsEverParked:
    """The invariant has to hold on every dispatch path, not just the claim's.

    ADR-0009 leans on "at most one run of a job is ever parked" — it is what removes the
    dedupe, the supersession rule and the cancel sweep. ``claim_due_jobs`` enforces it
    for scheduled occurrences, but ``run_now`` bypasses the claim entirely, so a few
    presses left several parked runs each holding its own live ask. Observed in testing
    as an authorization card that would not go away: it was a *different*, older parked
    run than the one just answered.
    """

    @pytest.mark.asyncio
    async def test_the_lookup_keys_on_answerability_not_status(self):
        """An answered run keeps `auth_required` forever; only the task id clears."""
        repo = ScheduledJobRepository()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(mappings=lambda: MagicMock(first=lambda: None)))

        assert await repo.answerable_parked_run(db, 15) is None

        sql = db.execute.await_args.args[0].text
        assert "parked_task_id IS NOT NULL" in sql, (
            "keying on status alone would find runs that have already been answered"
        )
        assert "status = 'auth_required'" in sql


class TestSubscriberAgentAccessIsCheckedAtDispatch:
    """ADR-0010: a definition's agent is checked against the SUBSCRIBER at every dispatch,
    because access can be revoked after the definition was shared. Missing access pauses
    that one subscription — a real pause, no failure count — and runs nothing."""

    @pytest.mark.asyncio
    async def test_missing_access_pauses_the_subscription_without_a_failure_count(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 1
        repo.complete_job = AsyncMock(return_value=(False, "Agent not accessible: …"))
        repo.complete_run = AsyncMock()
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.return_value = "token"
        access_check = AsyncMock(return_value=False)

        engine = _make_engine(repo=repo, token_service=token_service)
        engine._agent_access_check = access_check
        job = make_job(user_id="subscriber", owner_user_id="owner", sub_agent_id=42)

        with patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
            await engine._dispatch_job(job)

        dispatch.assert_not_called()
        access_check.assert_awaited_once()
        assert access_check.call_args.args[1:] == ("subscriber", 42)
        # A real pause: enabled off with the reason, written before the bookkeeping.
        repo.disable_subscription.assert_awaited_once()
        assert "not accessible" in repo.disable_subscription.call_args.args[2].lower()
        # Recorded as FAILED on the run, but neutral on the subscription's failure counter.
        assert repo.complete_run.call_args[1]["status"] == JobRunStatus.FAILED
        assert repo.complete_job.call_args[1]["status"] == JobRunStatus.INTERRUPTED
        assert repo.complete_job.call_args[1]["retry_at"] is None

    @pytest.mark.asyncio
    async def test_the_check_is_required_at_construction(self):
        with pytest.raises(TypeError):
            SchedulerEngine(  # type: ignore[call-arg]
                repo=AsyncMock(spec=ScheduledJobRepository),
                delivery_channel_repo=AsyncMock(spec=DeliveryChannelRepository),
                token_service=AsyncMock(spec=SchedulerTokenService),
                agent_runner_url="http://runner",
                db_session_factory=_make_mock_session_factory(),
            )

    @pytest.mark.asyncio
    async def test_a_resumed_run_is_checked_too(self):
        """A parked run can wait for days; access is judged when the answer arrives."""
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 9
        repo.complete_job = AsyncMock(return_value=(False, "Agent not accessible"))
        repo.complete_run = AsyncMock()
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.return_value = "token"
        engine = _make_engine(repo=repo, token_service=token_service)
        engine._agent_access_check = AsyncMock(return_value=False)
        job = make_job(user_id="subscriber", owner_user_id="owner", sub_agent_id=42)
        parked = ScheduledJobRun(
            id=8, job_id=job.id, started_at=datetime.now(timezone.utc), status=JobRunStatus.AUTH_REQUIRED,
            delivered=False, parked_task_id="task-1", conversation_id="ctx-1",
        )

        with patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
            await engine.resume_parked_run(job, parked, "approved", run_id=9)

        dispatch.assert_not_called()
        repo.disable_subscription.assert_awaited_once()
        assert repo.complete_run.call_args[1]["status"] == JobRunStatus.FAILED
        assert repo.complete_job.call_args[1]["status"] == JobRunStatus.INTERRUPTED

    @pytest.mark.asyncio
    async def test_an_allowed_subscriber_dispatches_as_before(self):
        repo = AsyncMock(spec=ScheduledJobRepository)
        repo.create_run.return_value = 1
        repo.complete_job = AsyncMock(return_value=(True, None))
        repo.complete_run = AsyncMock()
        token_service = AsyncMock(spec=SchedulerTokenService)
        token_service.get_access_token.return_value = "token"
        engine = _make_engine(repo=repo, token_service=token_service)

        with patch("console_backend.services.scheduler_engine.dispatch_streaming") as dispatch:
            dispatch.return_value = {"result": {"kind": "task", "status": {"state": "completed"}, "artifacts": []}}
            await engine._dispatch_job(make_job(sub_agent_id=42))

        dispatch.assert_called_once()
        repo.disable_subscription.assert_not_awaited()
