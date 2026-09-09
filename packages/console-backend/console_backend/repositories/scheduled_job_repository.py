"""Repository for scheduled jobs and their execution run history."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from croniter import croniter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditEntityType
from ..models.scheduled_job import (
    ConditionEvaluation,
    JobRunStatus,
    JobType,
    RunTrigger,
    ScheduledJob,
    ScheduledJobRun,
    ScheduleKind,
)
from ..models.user import User
from ..utils.timezones import resolve_timezone
from .base import AuditedRepository

logger = logging.getLogger(__name__)


def _row_to_scheduled_job(row: Any) -> ScheduledJob:
    """Convert a database row mapping to a ScheduledJob model."""
    return ScheduledJob(
        id=row["id"],
        user_id=row["user_id"],
        sub_agent_id=row["sub_agent_id"],
        name=row["name"],
        job_type=JobType(row["job_type"]),
        schedule_kind=ScheduleKind(row["schedule_kind"]),
        cron_expr=row["cron_expr"],
        timezone=row["timezone"],
        interval_seconds=row["interval_seconds"],
        run_at=row["run_at"],
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        retry_at=row.get("retry_at"),
        prompt=row.get("prompt"),
        notification_message=row.get("notification_message"),
        check_tool=row["check_tool"],
        check_args=row["check_args"],
        check_args_exprs=row.get("check_args_exprs"),
        cel_expr=row.get("cel_expr"),
        llm_condition=row.get("llm_condition"),
        destroy_after_trigger=row.get("destroy_after_trigger", True),
        last_check_result=row["last_check_result"],
        delivery_channel_id=row["delivery_channel_id"],
        voice_call=row.get("voice_call", False),
        enabled=row["enabled"],
        max_failures=row["max_failures"],
        consecutive_failures=row["consecutive_failures"],
        paused_reason=row["paused_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row.get("deleted_at"),
    )


def _row_to_run(row: Any) -> ScheduledJobRun:
    """Convert a database row mapping to a ScheduledJobRun model."""
    return ScheduledJobRun(
        id=row["id"],
        job_id=row["job_id"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        status=JobRunStatus(row["status"]),
        result_summary=row["result_summary"],
        error_message=row["error_message"],
        conversation_id=row.get("conversation_id"),
        delivered=row["delivered"],
        condition_evaluation=row.get("condition_evaluation"),
        last_seen_at=row.get("last_seen_at"),
        trigger=RunTrigger(row.get("trigger", RunTrigger.SCHEDULED.value)),
        notice_due_at=row.get("notice_due_at"),
    )


@dataclass(frozen=True)
class ClaimedJob:
    """A job handed to a scheduler by ``claim_due_jobs`` and why it was due.

    The trigger is decided by the claim itself rather than inferred afterwards, so the
    caller never depends on what the row looked like before the marker was consumed.
    """

    job: ScheduledJob
    trigger: RunTrigger


def compute_next_run(
    schedule_kind: ScheduleKind,
    cron_expr: str | None,
    interval_seconds: int | None,
    run_at: datetime | None,
    after: datetime | None = None,
    tz: str | None = None,
) -> datetime | None:
    """Compute the next scheduled run datetime (always returned in UTC).

    Cron wall-clock fields are interpreted in *tz* (IANA name; None/empty falls
    back to the DEFAULT_TIMEZONE deployment default), so "0 8 * * *" fires at
    08:00 local time across DST changes. Raises ValueError if *tz* cannot be
    resolved. Returns None for schedule_kind='once' — the job is done after
    the first run.
    """
    base = after or datetime.now(timezone.utc)

    if schedule_kind == ScheduleKind.CRON:
        assert cron_expr, "cron_expr required for cron schedule"
        zone = resolve_timezone(tz)
        cron = croniter(cron_expr, base.astimezone(zone))
        next_dt = cron.get_next(datetime)
        # During a DST fall-back the same wall-clock time exists twice and
        # croniter yields both folds. A wall-clock schedule must fire once, so
        # skip a fold-1 repeat whose first occurrence has already passed.
        while next_dt.fold and next_dt.replace(fold=0).astimezone(timezone.utc) <= base:
            next_dt = cron.get_next(datetime)
        return next_dt.astimezone(timezone.utc)

    if schedule_kind == ScheduleKind.INTERVAL:
        assert interval_seconds, "interval_seconds required for interval schedule"
        return base + timedelta(seconds=interval_seconds)

    # ScheduleKind.ONCE: no repeat
    return None


class ScheduledJobRepository(AuditedRepository):
    """Repository for scheduled jobs with claim-based execution and run history."""

    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.SCHEDULED_JOB,
            table_name="scheduled_jobs",
        )

    async def create_job(
        self,
        db: AsyncSession,
        actor: User,
        fields: dict[str, Any],
    ) -> int:
        """Create a new scheduled job. Returns the new job ID."""
        return await self.create(db=db, actor=actor, fields=fields, returning="id")

    async def get_job(self, db: AsyncSession, job_id: int) -> ScheduledJob | None:
        """Fetch a single job by ID."""
        result = await db.execute(
            text("SELECT * FROM scheduled_jobs WHERE id = :id AND deleted_at IS NULL"),
            {"id": job_id},
        )
        row = result.mappings().first()
        return _row_to_scheduled_job(row) if row else None

    async def list_jobs(self, db: AsyncSession, user_id: str) -> list[ScheduledJob]:
        """List all non-deleted jobs for a user, newest first."""
        result = await db.execute(
            text("""
                SELECT * FROM scheduled_jobs
                WHERE user_id = :user_id
                  AND deleted_at IS NULL
                ORDER BY created_at DESC
            """),
            {"user_id": user_id},
        )
        return [_row_to_scheduled_job(r) for r in result.mappings().all()]

    async def claim_due_jobs(self, db: AsyncSession, limit: int = 10) -> list[ClaimedJob]:
        """Claim up to *limit* due jobs using SELECT … FOR UPDATE SKIP LOCKED.

        Marks each claimed job as claimed by setting last_run_at = NOW() to prevent
        double-processing in a multi-instance deployment.  The caller is responsible
        for updating next_run_at once execution completes.

        Two wake-up reasons, and the returned trigger says which one fired. The
        schedule (``next_run_at``) is the ordinary one. A due ``retry_at`` is the
        second: the fresh attempt an interrupted run earns, put in the database
        rather than in the noticing process because the process best placed to
        notice an interruption is often the one dying. Whichever scheduler ticks
        next picks it up.

        The retry branch does not require ``enabled``: a ``once`` job is retired by
        ``complete_job`` the moment its occurrence is recorded, so requiring
        ``enabled`` would discard the very attempt the interruption earned.
        ``paused_reason IS NULL`` is what keeps that from resurrecting a job somebody
        stopped on purpose — every deliberate stop writes a reason, one-shot
        retirement does not.

        A job with a run still ``running`` is not claimable on either branch. The
        schedule does not advance until a run completes, so without this a process
        restart would re-claim a job through a stale ``next_run_at`` while its
        stranded run waits for the healer, and the interruption would then earn a
        retry on top — two extra attempts for one loss. A stranded run is released
        by the healer within about a minute; a healthy one keeps the job for as long
        as it runs, so runs of the same job never overlap.

        ``retry_at`` is cleared on claim, so an attempt is handed out once even if
        several schedulers tick together.
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                SELECT j.*,
                       (j.retry_at IS NOT NULL AND j.retry_at <= :now) AS via_retry
                FROM scheduled_jobs j
                WHERE j.deleted_at IS NULL
                  AND (
                        (j.enabled = TRUE AND j.next_run_at <= :now)
                     OR (j.retry_at IS NOT NULL AND j.retry_at <= :now AND j.paused_reason IS NULL)
                  )
                  AND NOT EXISTS (
                        SELECT 1 FROM scheduled_job_runs r
                        WHERE r.job_id = j.id AND r.status = 'running'
                  )
                ORDER BY COALESCE(j.retry_at, j.next_run_at) ASC
                LIMIT :limit
                FOR UPDATE OF j SKIP LOCKED
            """),
            {"now": now, "limit": limit},
        )
        rows = result.mappings().all()
        if not rows:
            return []

        # Stamp last_run_at so other workers skip these rows during execution, and
        # consume the retry marker in the same statement — the attempt is now this
        # process's to make, and leaving it set would hand it out again next tick.
        ids = [r["id"] for r in rows]
        await db.execute(
            text("UPDATE scheduled_jobs SET last_run_at = :now, retry_at = NULL WHERE id = ANY(:ids)"),
            {"now": now, "ids": ids},
        )
        return [
            ClaimedJob(
                job=_row_to_scheduled_job(r),
                trigger=RunTrigger.RETRY if r["via_retry"] else RunTrigger.SCHEDULED,
            )
            for r in rows
        ]

    async def complete_job(
        self,
        db: AsyncSession,
        job_id: int,
        status: JobRunStatus,
        next_run_at: datetime | None,
        last_check_result: dict[str, Any] | None = None,
        paused_reason: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        """Update a job after execution: advance schedule, track failures, auto-pause on threshold.

        Takes the run's status rather than a success flag because there are three
        outcomes, not two. Only a FAILED run moves ``consecutive_failures`` up and
        only a successful one resets it; an INTERRUPTED run leaves it alone in both
        directions. See docs/adr/0007-interrupted-runs-get-one-fresh-attempt.md.

        *retry_at* schedules the one fresh attempt an interruption earns. It is
        only ever written, never cleared here: runs of one job can complete out of
        order — a manual run finishing after the healer marked a scheduled one lost —
        and a completion that wiped the marker would silently cancel an attempt that
        was earned. The claim consumes it; pause and resume clear it.
        """
        now = datetime.now(timezone.utc)
        failed = status == JobRunStatus.FAILED
        success = status in (JobRunStatus.SUCCESS, JobRunStatus.CONDITION_NOT_MET)

        await db.execute(
            text("""
                UPDATE scheduled_jobs
                SET
                    consecutive_failures = CASE
                        WHEN :failed  THEN consecutive_failures + 1
                        WHEN :success THEN 0
                        ELSE consecutive_failures
                    END,
                    last_run_at          = :last_run_at,
                    next_run_at          = COALESCE(:next_run_at, next_run_at),
                    retry_at             = COALESCE(CAST(:retry_at AS timestamptz), retry_at),
                    enabled              = CASE
                        WHEN :next_run_at IS NULL                                        THEN FALSE
                        WHEN :failed AND (consecutive_failures + 1) >= max_failures      THEN FALSE
                        ELSE enabled
                    END,
                    paused_reason        = CASE
                        WHEN :failed AND (consecutive_failures + 1) >= max_failures
                            THEN 'Auto-paused after ' || max_failures || ' consecutive failures'
                        WHEN CAST(:paused_reason AS text) IS NOT NULL THEN CAST(:paused_reason AS text)
                        ELSE paused_reason
                    END,
                    last_check_result    = COALESCE(CAST(:last_check_result AS jsonb), last_check_result),
                    updated_at           = :now
                WHERE id = :job_id
            """),
            {
                "job_id": job_id,
                "failed": failed,
                "success": success,
                "last_run_at": now,
                "next_run_at": next_run_at,
                "retry_at": retry_at,
                "paused_reason": paused_reason,
                # `is not None`, not truthiness: `{}` is a real response (a tool with no
                # content returns one), and mapping it to NULL makes the COALESCE above
                # keep the previous payload — so `prev` never catches up and a
                # `result != prev` condition stays true on every poll.
                "last_check_result": (
                    json.dumps(last_check_result) if last_check_result is not None else None
                ),
                "now": now,
            },
        )

    async def update_job(
        self,
        db: AsyncSession,
        actor: User,
        job_id: int,
        fields: dict[str, Any],
    ) -> None:
        """Update job fields with audit logging."""
        await self.update(db=db, actor=actor, entity_id=job_id, fields=fields)

    async def delete_job(
        self,
        db: AsyncSession,
        actor: User,
        job_id: int,
    ) -> None:
        """Soft-delete a scheduled job."""
        await self.delete(db=db, actor=actor, entity_id=job_id)

    async def create_run(
        self,
        db: AsyncSession,
        job_id: int,
        trigger: RunTrigger = RunTrigger.SCHEDULED,
    ) -> int:
        """Insert a new 'running' run record. Returns run ID.

        *trigger* is recorded on the row because the healer, which may run in a
        process that never saw this dispatch, decides from it what the run's
        interruption is worth. ``last_seen_at`` starts at insert time so a run is
        never stale before its first heartbeat.
        """
        result = await db.execute(
            text("""
                INSERT INTO scheduled_job_runs (job_id, started_at, status, last_seen_at, trigger)
                VALUES (:job_id, NOW(), 'running', NOW(), :trigger)
                RETURNING id
            """),
            {"job_id": job_id, "trigger": trigger.value},
        )
        row = result.mappings().first()
        assert row is not None
        return row["id"]

    async def touch_run(self, db: AsyncSession, run_id: int) -> None:
        """Record that the process dispatching *run_id* is still alive.

        The healer sweeps on staleness of this timestamp rather than on the run's
        age, which is what lets a legitimately slow run take as long as it needs
        while an abandoned one is caught in about a minute.
        """
        await db.execute(
            text("UPDATE scheduled_job_runs SET last_seen_at = NOW() WHERE id = :run_id"),
            {"run_id": run_id},
        )

    async def claim_due_notices(
        self,
        db: AsyncSession,
        next_attempt_at: datetime,
        give_up_before: datetime,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Claim owed notices that are due; return ``{run_id, job_id}`` for each.

        Claiming pushes ``notice_due_at`` to *next_attempt_at* in the same statement, so
        a notice is attempted once per round even with several schedulers ticking, and a
        failed attempt is naturally retried later without any attempt counter. The caller
        clears the marker on success.

        Two kinds of notice are abandoned rather than claimed, in the same statement.

        One whose run completed before *give_up_before*: past some age the news has
        stopped being useful, and a notifier still trying through a long outage is how one
        unhealthy process becomes a stampede.

        One the job has already moved past — any later run of the same job has completed.
        Delivering it then would contradict newer news the user already has, arriving
        after a fresh result to say the job could not run. Supersession is keyed on a
        later run having *completed*, not on the schedule having come round: if the next
        run is lost too, nothing has superseded anything and the notice is still owed.
        Ordering by ``(completed_at, id)`` also means that when an outage costs a job
        several runs, only the newest owed notice survives — one message, not a burst.
        """
        # Data-modifying CTEs all see the snapshot from before the statement, so `due`
        # excludes `abandoned` explicitly rather than relying on the NULL it just wrote.
        result = await db.execute(
            text("""
                WITH abandoned AS (
                    UPDATE scheduled_job_runs r
                    SET notice_due_at = NULL
                    WHERE r.notice_due_at IS NOT NULL
                      AND (
                            r.completed_at <= :give_up_before
                         OR EXISTS (
                                SELECT 1 FROM scheduled_job_runs newer
                                WHERE newer.job_id = r.job_id
                                  AND newer.completed_at IS NOT NULL
                                  AND (newer.completed_at, newer.id) > (r.completed_at, r.id)
                            )
                      )
                    RETURNING r.id, r.completed_at <= :give_up_before AS too_old
                ), due AS (
                    SELECT id
                    FROM scheduled_job_runs
                    WHERE notice_due_at IS NOT NULL
                      AND notice_due_at <= NOW()
                      AND id NOT IN (SELECT id FROM abandoned)
                    ORDER BY notice_due_at
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                ), claimed AS (
                    UPDATE scheduled_job_runs r
                    SET notice_due_at = :next_attempt_at
                    FROM due
                    WHERE r.id = due.id
                    RETURNING r.id, r.job_id
                )
                SELECT 'abandoned' AS outcome, id, NULL::bigint AS job_id, too_old FROM abandoned
                UNION ALL
                SELECT 'claimed', id, job_id, NULL FROM claimed
            """),
            {"give_up_before": give_up_before, "limit": limit, "next_attempt_at": next_attempt_at},
        )
        claimed: list[dict[str, Any]] = []
        for row in result.mappings().all():
            if row["outcome"] == "claimed":
                claimed.append({"run_id": row["id"], "job_id": row["job_id"]})
            else:
                logger.warning(
                    "Run %s: dropped the recovery notice (%s)",
                    row["id"],
                    "too old to be useful" if row["too_old"] else "superseded by a later run",
                )
        return claimed

    async def clear_notice(self, db: AsyncSession, run_id: int) -> None:
        """Mark the notice for *run_id* as no longer owed."""
        await db.execute(
            text("UPDATE scheduled_job_runs SET notice_due_at = NULL WHERE id = :run_id"),
            {"run_id": run_id},
        )

    async def interrupt_stale_runs(
        self,
        db: AsyncSession,
        stale_after_seconds: int,
        exclude_run_ids: list[int],
        retry_at: datetime,
        notice_due_at: datetime,
        heartbeatless_after_seconds: int,
    ) -> list[int]:
        """Mark runs whose dispatcher stopped reporting as interrupted; return their ids.

        A run is stale when its heartbeat (``last_seen_at``) is older than
        *stale_after_seconds*. A run with no heartbeat at all was written by a process
        on a release without one and may still be executing there, so it is judged by
        age against the far longer *heartbeatless_after_seconds* instead — the
        pre-heartbeat bound this replaced. Runs in *exclude_run_ids* are this
        process's own and are never touched.

        Each interrupted ``scheduled`` run earns its job one fresh attempt at
        *retry_at*, unless the job carries a ``paused_reason`` — those were stopped on
        purpose and are not quietly resumed. A stale ``retry`` run has exhausted
        recovery, so it is marked as owing the user a notice at *notice_due_at*
        instead. A ``manual`` run earns neither: the user was present.
        """
        result = await db.execute(
            text("""
                WITH stale AS (
                    UPDATE scheduled_job_runs
                    SET status        = 'interrupted',
                        completed_at  = NOW(),
                        error_message = 'The process running this job stopped before it finished',
                        notice_due_at = CASE WHEN trigger = 'retry' THEN :notice_due_at ELSE notice_due_at END
                    WHERE status = 'running'
                      AND (
                            (last_seen_at IS NOT NULL
                             AND last_seen_at < NOW() - make_interval(secs => :stale_after))
                         OR (last_seen_at IS NULL
                             AND started_at < NOW() - make_interval(secs => :heartbeatless_after))
                      )
                      AND NOT (id = ANY(:exclude))
                    RETURNING id, job_id, trigger
                ), retried AS (
                    UPDATE scheduled_jobs j
                    SET retry_at   = :retry_at,
                        updated_at = NOW()
                    FROM stale
                    WHERE j.id = stale.job_id
                      AND stale.trigger = 'scheduled'
                      AND j.deleted_at IS NULL
                      AND j.paused_reason IS NULL
                    RETURNING j.id
                )
                SELECT id FROM stale
            """),
            {
                "stale_after": stale_after_seconds,
                "heartbeatless_after": heartbeatless_after_seconds,
                "exclude": exclude_run_ids,
                "retry_at": retry_at,
                "notice_due_at": notice_due_at,
            },
        )
        return [r["id"] for r in result.mappings().all()]

    async def complete_run(
        self,
        db: AsyncSession,
        run_id: int,
        status: JobRunStatus,
        result_summary: str | None = None,
        error_message: str | None = None,
        conversation_id: str | None = None,
        delivered: bool = False,
        condition_evaluation: ConditionEvaluation | None = None,
        notice_due_at: datetime | None = None,
    ) -> bool:
        """Finalise a run record with execution outcome. Returns whether a row changed.

        Only a run still ``running`` is finalised. A run the healer has already called
        interrupted stays interrupted even if its dispatcher turns out to be alive and
        finishes: the retry it earned is already on its way, and a row flipping back to
        success would hide that the job ran twice.

        *notice_due_at* records, in the same write, that the user is owed the notice
        that this run was lost for good. Same statement on purpose: the process
        recording an interruption may be the one dying, and a run that is interrupted
        but owes nothing would leave the user untold.
        """
        result = await db.execute(
            text("""
                UPDATE scheduled_job_runs
                SET
                    completed_at     = NOW(),
                    status           = :status,
                    result_summary   = :result_summary,
                    error_message    = :error_message,
                    conversation_id  = :conversation_id,
                    delivered        = :delivered,
                    condition_evaluation = :condition_evaluation,
                    notice_due_at    = COALESCE(CAST(:notice_due_at AS timestamptz), notice_due_at)
                WHERE id = :run_id
                  AND status = 'running'
            """),
            {
                "run_id": run_id,
                "status": status.value,
                "result_summary": result_summary,
                "error_message": error_message,
                "conversation_id": conversation_id,
                "delivered": delivered,
                # mode="json" so the stored form is exactly what ScheduledJobRun will
                # validate when it is read back.
                "condition_evaluation": (
                    json.dumps(condition_evaluation.model_dump(mode="json"))
                    if condition_evaluation is not None
                    else None
                ),
                "notice_due_at": notice_due_at,
            },
        )
        return result.rowcount > 0

    async def close_run_minimally(
        self,
        db: AsyncSession,
        run_id: int,
        status: JobRunStatus,
        error_message: str | None,
    ) -> None:
        """Record only that *run_id* ended, touching no column added since the run table was created.

        The fallback for when ``complete_run`` fails. The outage that shaped ``_finalize``
        was exactly that — a write failing on a column the deployed schema did not have —
        and a run left ``running`` after it has finished is no longer harmless: the
        healer sweeps it, calls it interrupted, and re-executes a job whose result the
        user already has.
        """
        await db.execute(
            text("""
                UPDATE scheduled_job_runs
                SET completed_at = NOW(), status = :status, error_message = :error_message
                WHERE id = :run_id AND status = 'running'
            """),
            {"run_id": run_id, "status": status.value, "error_message": error_message},
        )

    async def get_run(
        self,
        db: AsyncSession,
        job_id: int,
        run_id: int,
    ) -> ScheduledJobRun | None:
        """Fetch a single run of a job by id, regardless of age."""
        result = await db.execute(
            text("""
                SELECT * FROM scheduled_job_runs
                WHERE id = :run_id AND job_id = :job_id
            """),
            {"run_id": run_id, "job_id": job_id},
        )
        row = result.mappings().first()
        return _row_to_run(row) if row is not None else None

    async def list_runs(
        self,
        db: AsyncSession,
        job_id: int,
        limit: int = 50,
    ) -> list[ScheduledJobRun]:
        """Fetch the most recent runs for a job, newest first."""
        result = await db.execute(
            text("""
                SELECT * FROM scheduled_job_runs
                WHERE job_id = :job_id
                ORDER BY started_at DESC
                LIMIT :limit
            """),
            {"job_id": job_id, "limit": limit},
        )
        return [_row_to_run(r) for r in result.mappings().all()]
