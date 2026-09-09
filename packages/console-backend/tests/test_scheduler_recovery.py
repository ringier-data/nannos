"""Recovery for runs interrupted by a process death.

Real-database tests: the behaviour under test is almost entirely SQL — a CASE that
must not move a counter, and a claim predicate with a second wake-up reason — so a
mocked repository would assert nothing. See
docs/adr/0007-interrupted-runs-get-one-fresh-attempt.md.
"""

from datetime import datetime, timedelta, timezone

import pytest
from console_backend.models.scheduled_job import JobRunStatus, RunTrigger
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.scheduler_helpers import job_retry_at, run_status, seed_job, seed_run


class TestFailureAccounting:
    """An interruption is evidence about the runtime, not about the job."""

    @pytest.mark.asyncio
    async def test_interruption_does_not_move_the_failure_counter(self, pg_session: AsyncSession):
        """Neither incremented (churn would auto-pause a healthy job) nor reset (it would
        launder a real failure streak)."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "counter", consecutive_failures=2)
        await pg_session.commit()

        async def _failures() -> int:
            r = await pg_session.execute(
                text("SELECT consecutive_failures FROM scheduled_jobs WHERE id = :id"), {"id": job_id}
            )
            return r.scalar_one()

        await repo.complete_job(
            db=pg_session,
            job_id=job_id,
            status=JobRunStatus.INTERRUPTED,
            next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),
            retry_at=datetime.now(timezone.utc),
        )
        await pg_session.commit()
        assert await _failures() == 2

        await repo.complete_job(
            db=pg_session,
            job_id=job_id,
            status=JobRunStatus.FAILED,
            next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await pg_session.commit()
        assert await _failures() == 3, "a genuine failure still counts"

    @pytest.mark.asyncio
    async def test_interruption_never_auto_pauses(self, pg_session: AsyncSession):
        """A job one failure short of max_failures survives any number of interruptions."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "nopause", consecutive_failures=2)  # max_failures = 3
        await pg_session.commit()

        for _ in range(3):
            await repo.complete_job(
                db=pg_session,
                job_id=job_id,
                status=JobRunStatus.INTERRUPTED,
                next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),
                retry_at=datetime.now(timezone.utc),
            )
        await pg_session.commit()

        r = await pg_session.execute(
            text("SELECT enabled, paused_reason FROM scheduled_jobs WHERE id = :id"), {"id": job_id}
        )
        row = r.mappings().first()
        assert row["enabled"] is True
        assert row["paused_reason"] is None


class TestRetryMarkerSurvivesOtherCompletions:
    """complete_job only ever writes retry_at, never clears it."""

    @pytest.mark.asyncio
    async def test_a_normal_completion_keeps_an_earned_retry(self, pg_session: AsyncSession):
        """Runs of one job complete out of order — a manual run finishing after the healer
        marked a scheduled one lost. A completion that wiped the marker would silently
        cancel an attempt that was earned."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "keep", retry_at="NOW() + INTERVAL '1 minute'")
        await pg_session.commit()

        await repo.complete_job(
            db=pg_session,
            job_id=job_id,
            status=JobRunStatus.SUCCESS,
            next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await pg_session.commit()

        assert await job_retry_at(pg_session, job_id) is not None


class TestRetryClaiming:
    """retry_at is the claim loop's second wake-up reason."""

    @pytest.mark.asyncio
    async def test_due_retry_is_claimed_and_the_marker_consumed(self, pg_session: AsyncSession):
        """Claimed even though next_run_at is an hour away, and handed out only once."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "claim", retry_at="NOW() - INTERVAL '1 minute'")
        await pg_session.commit()

        claimed = await repo.claim_due_jobs(pg_session, limit=10)
        await pg_session.commit()

        mine = next(c for c in claimed if c.job.id == job_id)
        # The claim says why the job was due, so the engine never infers it from a
        # marker the same statement has just consumed.
        assert mine.trigger is RunTrigger.RETRY
        assert await job_retry_at(pg_session, job_id) is None

    @pytest.mark.asyncio
    async def test_a_job_due_on_its_schedule_is_a_scheduled_claim(self, pg_session: AsyncSession):
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "sched", next_run_at="NOW() - INTERVAL '1 minute'")
        await pg_session.commit()

        claimed = await repo.claim_due_jobs(pg_session, limit=10)
        await pg_session.commit()

        assert next(c for c in claimed if c.job.id == job_id).trigger is RunTrigger.SCHEDULED

    @pytest.mark.asyncio
    async def test_a_job_with_a_run_still_running_is_not_claimed(self, pg_session: AsyncSession):
        """The schedule does not advance until a run completes, so after a restart a
        stranded run would let its job be re-claimed through the stale next_run_at —
        and then earn a retry on top when the healer sweeps it. One loss, one attempt."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "running", next_run_at="NOW() - INTERVAL '1 minute'")
        run_id = await seed_run(pg_session, job_id, started_at="NOW() - INTERVAL '30 seconds'")
        await pg_session.commit()

        claimed = await repo.claim_due_jobs(pg_session, limit=10)
        await pg_session.commit()
        assert job_id not in [c.job.id for c in claimed]

        # Once the healer (or the dispatcher) has closed the run, the job is claimable again.
        await pg_session.execute(
            text("UPDATE scheduled_job_runs SET status = 'interrupted', completed_at = NOW() WHERE id = :id"),
            {"id": run_id},
        )
        await pg_session.commit()
        claimed = await repo.claim_due_jobs(pg_session, limit=10)
        await pg_session.commit()
        assert job_id in [c.job.id for c in claimed]

    @pytest.mark.asyncio
    async def test_future_retry_is_not_claimed(self, pg_session: AsyncSession):
        """The delay before a fresh attempt is real: a runner still restarting is not
        immediately handed the same work."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "future", retry_at="NOW() + INTERVAL '5 minutes'")
        await pg_session.commit()

        claimed = await repo.claim_due_jobs(pg_session, limit=10)
        await pg_session.commit()

        assert job_id not in [c.job.id for c in claimed]

    @pytest.mark.asyncio
    async def test_retry_survives_a_retired_once_job(self, pg_session: AsyncSession):
        """A 'once' job is retired the moment its occurrence is recorded, so the retry
        branch cannot require `enabled` — that is the case it exists for."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(
            pg_session,
            "once",
            schedule_kind="once",
            enabled="false",  # compute_next_run returns None for 'once', so complete_job disables it
            retry_at="NOW() - INTERVAL '1 minute'",
        )
        await pg_session.commit()

        claimed = await repo.claim_due_jobs(pg_session, limit=10)
        await pg_session.commit()

        assert job_id in [c.job.id for c in claimed]

    @pytest.mark.asyncio
    async def test_retry_does_not_resurrect_a_deliberately_stopped_job(self, pg_session: AsyncSession):
        """paused_reason separates one-shot retirement from somebody stopping the job."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(
            pg_session,
            "paused",
            enabled="false",
            paused_reason="Manually paused",
            retry_at="NOW() - INTERVAL '1 minute'",
        )
        await pg_session.commit()

        claimed = await repo.claim_due_jobs(pg_session, limit=10)
        await pg_session.commit()

        assert job_id not in [c.job.id for c in claimed]


class TestHeartbeat:
    """touch_run is what lets the healer sweep on staleness rather than age."""

    @pytest.mark.asyncio
    async def test_touch_run_refreshes_liveness(self, pg_session: AsyncSession):
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "beat")
        run_id = await repo.create_run(pg_session, job_id)
        await pg_session.execute(
            text("UPDATE scheduled_job_runs SET last_seen_at = NOW() - INTERVAL '1 hour' WHERE id = :id"),
            {"id": run_id},
        )
        await pg_session.commit()

        await repo.touch_run(pg_session, run_id)
        await pg_session.commit()

        r = await pg_session.execute(
            text("SELECT NOW() - last_seen_at < INTERVAL '5 seconds' FROM scheduled_job_runs WHERE id = :id"),
            {"id": run_id},
        )
        assert r.scalar_one() is True

    @pytest.mark.asyncio
    async def test_create_run_starts_alive_and_scheduled(self, pg_session: AsyncSession):
        """A run must never be stale before its first heartbeat."""
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "fresh")
        run_id = await repo.create_run(pg_session, job_id)
        manual_id = await repo.create_run(pg_session, job_id, trigger=RunTrigger.MANUAL)
        await pg_session.commit()

        r = await pg_session.execute(
            text("SELECT last_seen_at IS NOT NULL AS alive, trigger FROM scheduled_job_runs WHERE id = :id"),
            {"id": run_id},
        )
        row = r.mappings().first()
        assert row["alive"] is True
        assert row["trigger"] == "scheduled"
        r = await pg_session.execute(text("SELECT trigger FROM scheduled_job_runs WHERE id = :id"), {"id": manual_id})
        assert r.scalar_one() == "manual"

    @pytest.mark.asyncio
    async def test_complete_run_records_the_notice_in_the_same_write(self, pg_session: AsyncSession):
        repo = ScheduledJobRepository()
        job_id = await seed_job(pg_session, "notice")
        run_id = await repo.create_run(pg_session, job_id, trigger=RunTrigger.RETRY)
        await pg_session.commit()

        due = datetime.now(timezone.utc) + timedelta(seconds=90)
        changed = await repo.complete_run(pg_session, run_id, JobRunStatus.INTERRUPTED, notice_due_at=due)
        await pg_session.commit()

        assert changed is True
        assert await run_status(pg_session, run_id) == "interrupted"
        r = await pg_session.execute(
            text("SELECT notice_due_at FROM scheduled_job_runs WHERE id = :id"), {"id": run_id}
        )
        assert r.scalar_one() == due


class TestNoticeDebt:
    """The notice owed when recovery is exhausted, claimed and cleared in SQL."""

    @staticmethod
    async def _owed_run(pg_session: AsyncSession, suffix: str, *, due: str, completed: str) -> tuple[int, int]:
        job_id = await seed_job(pg_session, suffix)
        run_id = await seed_run(
            pg_session,
            job_id,
            status="interrupted",
            started_at="NOW() - INTERVAL '3 hours'",
            completed_at=completed,
            trigger="retry",
            notice_due_at=due,
        )
        return job_id, run_id

    @pytest.mark.asyncio
    async def test_due_notice_is_claimed_and_pushed_out(self, pg_session: AsyncSession):
        """Claiming defers the next attempt, so one round makes one attempt and a
        failure is retried later without an attempt counter."""
        repo = ScheduledJobRepository()
        now = datetime.now(timezone.utc)
        _, run_id = await self._owed_run(
            pg_session, "due", due="NOW() - INTERVAL '1 minute'", completed="NOW() - INTERVAL '2 minutes'"
        )
        await pg_session.commit()

        claimed = await repo.claim_due_notices(
            pg_session,
            next_attempt_at=now + timedelta(minutes=5),
            give_up_before=now - timedelta(hours=1),
        )
        await pg_session.commit()

        assert [c["run_id"] for c in claimed] == [run_id]
        r = await pg_session.execute(
            text("SELECT notice_due_at > NOW() FROM scheduled_job_runs WHERE id = :id"), {"id": run_id}
        )
        assert r.scalar_one() is True

        await repo.clear_notice(pg_session, run_id)
        await pg_session.commit()
        r = await pg_session.execute(
            text("SELECT notice_due_at FROM scheduled_job_runs WHERE id = :id"), {"id": run_id}
        )
        assert r.scalar_one() is None

    @pytest.mark.asyncio
    async def test_a_future_notice_is_not_claimed(self, pg_session: AsyncSession):
        """The delay before the first attempt is the point: it lets the agent come back."""
        repo = ScheduledJobRepository()
        now = datetime.now(timezone.utc)
        _, run_id = await self._owed_run(
            pg_session, "later", due="NOW() + INTERVAL '2 minutes'", completed="NOW()"
        )
        await pg_session.commit()

        claimed = await repo.claim_due_notices(
            pg_session,
            next_attempt_at=now + timedelta(minutes=5),
            give_up_before=now - timedelta(hours=1),
        )
        await pg_session.commit()

        assert run_id not in [c["run_id"] for c in claimed]

    @pytest.mark.asyncio
    async def test_an_old_notice_is_abandoned(self, pg_session: AsyncSession):
        """Past some age the news has stopped being useful, and a notifier still trying
        through a long outage is its own small stampede."""
        repo = ScheduledJobRepository()
        now = datetime.now(timezone.utc)
        _, run_id = await self._owed_run(
            pg_session, "stale", due="NOW() - INTERVAL '1 minute'", completed="NOW() - INTERVAL '3 hours'"
        )
        await pg_session.commit()

        claimed = await repo.claim_due_notices(
            pg_session,
            next_attempt_at=now + timedelta(minutes=5),
            give_up_before=now - timedelta(hours=1),
        )
        await pg_session.commit()

        assert run_id not in [c["run_id"] for c in claimed]
        r = await pg_session.execute(
            text("SELECT notice_due_at FROM scheduled_job_runs WHERE id = :id"), {"id": run_id}
        )
        assert r.scalar_one() is None, "an abandoned debt must not be reclaimed forever"

    @pytest.mark.asyncio
    async def test_a_later_completed_run_supersedes_the_notice(self, pg_session: AsyncSession):
        """Delivering it now would contradict newer news the user already has: a fresh
        result, followed by a message saying the job could not run."""
        repo = ScheduledJobRepository()
        now = datetime.now(timezone.utc)
        job_id, run_id = await self._owed_run(
            pg_session, "superseded", due="NOW() - INTERVAL '1 minute'", completed="NOW() - INTERVAL '10 minutes'"
        )
        # The next occurrence ran and delivered.
        await pg_session.execute(
            text("""
                INSERT INTO scheduled_job_runs (job_id, started_at, completed_at, status, delivered)
                VALUES (:job_id, NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '4 minutes', 'success', TRUE)
            """),
            {"job_id": job_id},
        )
        await pg_session.commit()

        claimed = await repo.claim_due_notices(
            pg_session,
            next_attempt_at=now + timedelta(minutes=5),
            give_up_before=now - timedelta(hours=1),
        )
        await pg_session.commit()

        assert run_id not in [c["run_id"] for c in claimed]
        r = await pg_session.execute(
            text("SELECT notice_due_at FROM scheduled_job_runs WHERE id = :id"), {"id": run_id}
        )
        assert r.scalar_one() is None

    @pytest.mark.asyncio
    async def test_a_later_lost_run_supersedes_nothing(self, pg_session: AsyncSession):
        """Keyed on a later run *completing*, not on the schedule coming round: if the
        next run is lost too, nothing has superseded anything."""
        repo = ScheduledJobRepository()
        now = datetime.now(timezone.utc)
        job_id, run_id = await self._owed_run(
            pg_session, "stillrunning", due="NOW() - INTERVAL '1 minute'", completed="NOW() - INTERVAL '10 minutes'"
        )
        # The next occurrence started and is still in flight — nothing delivered.
        await pg_session.execute(
            text("""
                INSERT INTO scheduled_job_runs (job_id, started_at, status, last_seen_at)
                VALUES (:job_id, NOW() - INTERVAL '5 minutes', 'running', NOW())
            """),
            {"job_id": job_id},
        )
        await pg_session.commit()

        claimed = await repo.claim_due_notices(
            pg_session,
            next_attempt_at=now + timedelta(minutes=5),
            give_up_before=now - timedelta(hours=1),
        )
        await pg_session.commit()

        assert run_id in [c["run_id"] for c in claimed]

    @pytest.mark.asyncio
    async def test_an_outage_costing_several_runs_sends_one_notice(self, pg_session: AsyncSession):
        """Only the newest owed notice survives — one message, not a burst."""
        repo = ScheduledJobRepository()
        now = datetime.now(timezone.utc)
        job_id = await seed_job(pg_session, "burst")

        run_ids = []
        for minutes_ago in (30, 20, 10):
            run_ids.append(
                await seed_run(
                    pg_session,
                    job_id,
                    status="interrupted",
                    started_at=f"NOW() - INTERVAL '{minutes_ago + 5} minutes'",
                    completed_at=f"NOW() - INTERVAL '{minutes_ago} minutes'",
                    trigger="retry",
                    notice_due_at="NOW() - INTERVAL '1 minute'",
                )
            )
        await pg_session.commit()

        claimed = await repo.claim_due_notices(
            pg_session,
            next_attempt_at=now + timedelta(minutes=5),
            give_up_before=now - timedelta(hours=1),
        )
        await pg_session.commit()

        assert [c["run_id"] for c in claimed] == [run_ids[-1]], "the newest loss is the one worth telling"
        r = await pg_session.execute(
            text("SELECT COUNT(*) FROM scheduled_job_runs WHERE id = ANY(:ids) AND notice_due_at IS NULL"),
            {"ids": run_ids[:-1]},
        )
        assert r.scalar_one() == 2

    @pytest.mark.asyncio
    async def test_healer_owes_a_notice_only_for_an_exhausted_retry(self, pg_session: AsyncSession):
        """The healer is the only witness when the scheduler itself was what died."""
        repo = ScheduledJobRepository()
        now = datetime.now(timezone.utc)
        job_id = await seed_job(pg_session, "healnotice")

        async def _stale_run(trigger: str) -> int:
            return await seed_run(
                pg_session,
                job_id,
                started_at="NOW() - INTERVAL '2 hours'",
                last_seen_at="NOW() - INTERVAL '1 hour'",
                trigger=trigger,
            )

        retry_run = await _stale_run("retry")
        first_run = await _stale_run("scheduled")
        await pg_session.commit()

        await repo.interrupt_stale_runs(
            pg_session,
            stale_after_seconds=60,
            heartbeatless_after_seconds=1800,
            exclude_run_ids=[],
            retry_at=now + timedelta(seconds=60),
            notice_due_at=now + timedelta(seconds=90),
        )
        await pg_session.commit()

        r = await pg_session.execute(
            text("SELECT id, notice_due_at IS NOT NULL AS owed FROM scheduled_job_runs WHERE id = ANY(:ids)"),
            {"ids": [retry_run, first_run]},
        )
        owed = {row["id"]: row["owed"] for row in r.mappings().all()}
        assert owed[retry_run] is True, "recovery is exhausted; nothing else will tell the user"
        assert owed[first_run] is False, "this one earned a retry — silence is correct"
