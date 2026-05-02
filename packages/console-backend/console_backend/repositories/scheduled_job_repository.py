"""Repository for scheduled jobs and their execution run history."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from croniter import croniter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditEntityType
from ..models.scheduled_job import JobRunStatus, JobType, ScheduledJob, ScheduledJobRun, ScheduleKind
from ..models.user import User
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
        interval_seconds=row["interval_seconds"],
        run_at=row["run_at"],
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        prompt=row.get("prompt"),
        notification_message=row.get("notification_message"),
        check_tool=row["check_tool"],
        check_args=row["check_args"],
        condition_expr=row["condition_expr"],
        expected_value=row.get("expected_value"),
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
    )


def compute_next_run(
    schedule_kind: ScheduleKind,
    cron_expr: str | None,
    interval_seconds: int | None,
    run_at: datetime | None,
    after: datetime | None = None,
) -> datetime | None:
    """Compute the next scheduled run datetime.

    Returns None for schedule_kind='once' — the job is done after the first run.
    """
    base = after or datetime.now(timezone.utc)

    if schedule_kind == ScheduleKind.CRON:
        assert cron_expr, "cron_expr required for cron schedule"
        cron = croniter(cron_expr, base)
        return cron.get_next(datetime)

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

    async def claim_due_jobs(self, db: AsyncSession, limit: int = 10) -> list[ScheduledJob]:
        """Claim up to *limit* due jobs using SELECT … FOR UPDATE SKIP LOCKED.

        Marks each claimed job as claimed by setting last_run_at = NOW() to prevent
        double-processing in a multi-instance deployment.  The caller is responsible
        for updating next_run_at once execution completes.
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                SELECT * FROM scheduled_jobs
                WHERE deleted_at IS NULL
                  AND enabled = TRUE
                  AND next_run_at <= :now
                ORDER BY next_run_at ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            """),
            {"now": now, "limit": limit},
        )
        rows = result.mappings().all()
        if not rows:
            return []

        # Stamp last_run_at so other workers skip these rows during execution
        ids = [r["id"] for r in rows]
        await db.execute(
            text("UPDATE scheduled_jobs SET last_run_at = :now WHERE id = ANY(:ids)"),
            {"now": now, "ids": ids},
        )
        return [_row_to_scheduled_job(r) for r in rows]

    async def complete_job(
        self,
        db: AsyncSession,
        job_id: int,
        success: bool,
        next_run_at: datetime | None,
        last_check_result: dict[str, Any] | None = None,
        paused_reason: str | None = None,
    ) -> None:
        """Update a job after execution: advance schedule, track failures, auto-pause on threshold."""
        now = datetime.now(timezone.utc)

        if success:
            fields: dict[str, Any] = {
                "consecutive_failures": 0,
                "last_run_at": now,
                "updated_at": now,
            }
        else:
            fields = {
                "consecutive_failures": text("consecutive_failures + 1"),
                "last_run_at": now,
                "updated_at": now,
            }

        if last_check_result is not None:
            fields["last_check_result"] = json.dumps(last_check_result)

        if next_run_at is not None:
            fields["next_run_at"] = next_run_at
            fields["enabled"] = True
        else:
            # schedule_kind='once' or max failures reached — disable
            fields["enabled"] = False

        if paused_reason is not None:
            fields["paused_reason"] = paused_reason
            fields["enabled"] = False

        # Check if max_failures threshold is crossed (done via raw SQL to avoid a
        # round-trip fetch)
        await db.execute(
            text("""
                UPDATE scheduled_jobs
                SET
                    consecutive_failures = CASE
                        WHEN :success THEN 0
                        ELSE consecutive_failures + 1
                    END,
                    last_run_at          = :last_run_at,
                    next_run_at          = COALESCE(:next_run_at, next_run_at),
                    enabled              = CASE
                        WHEN :next_run_at IS NULL          THEN FALSE
                        WHEN NOT :success AND (consecutive_failures + 1) >= max_failures THEN FALSE
                        ELSE enabled
                    END,
                    paused_reason        = CASE
                        WHEN NOT :success AND (consecutive_failures + 1) >= max_failures
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
                "success": success,
                "last_run_at": now,
                "next_run_at": next_run_at,
                "paused_reason": paused_reason,
                "last_check_result": json.dumps(last_check_result) if last_check_result else None,
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
    ) -> int:
        """Insert a new 'running' run record. Returns run ID."""
        result = await db.execute(
            text("""
                INSERT INTO scheduled_job_runs (job_id, started_at, status)
                VALUES (:job_id, NOW(), 'running')
                RETURNING id
            """),
            {"job_id": job_id},
        )
        row = result.mappings().first()
        assert row is not None
        return row["id"]

    async def complete_run(
        self,
        db: AsyncSession,
        run_id: int,
        status: JobRunStatus,
        result_summary: str | None = None,
        error_message: str | None = None,
        conversation_id: str | None = None,
        delivered: bool = False,
    ) -> None:
        """Finalise a run record with execution outcome."""
        await db.execute(
            text("""
                UPDATE scheduled_job_runs
                SET
                    completed_at     = NOW(),
                    status           = :status,
                    result_summary   = :result_summary,
                    error_message    = :error_message,
                    conversation_id  = :conversation_id,
                    delivered        = :delivered
                WHERE id = :run_id
            """),
            {
                "run_id": run_id,
                "status": status.value,
                "result_summary": result_summary,
                "error_message": error_message,
                "conversation_id": conversation_id,
                "delivered": delivered,
            },
        )

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
