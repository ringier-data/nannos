"""Shared fixtures-as-functions for the scheduler test modules.

Two things every scheduler test needs and none should hand-write: a ScheduledJob model
with sane defaults, and a user plus job row in a real database.
"""

from datetime import datetime, timedelta, timezone

from console_backend.models.scheduled_job import JobType, ScheduledJob, ScheduleKind
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def make_job(
    job_id: int = 1,
    user_id: str = "user-abc",
    job_type: JobType = JobType.TASK,
    sub_agent_id: int | None = 42,
    schedule_kind: ScheduleKind = ScheduleKind.INTERVAL,
    interval_seconds: int | None = 3600,
    cel_expr: str | None = None,
    llm_condition: str | None = None,
    destroy_after_trigger: bool = True,
    max_failures: int = 3,
    consecutive_failures: int = 0,
    delivery_channel_id: int | None = None,
    name: str = "Test Job",
) -> ScheduledJob:
    """An enabled interval task job due in an hour, unless told otherwise."""
    now = datetime.now(timezone.utc)
    return ScheduledJob(
        id=job_id,
        user_id=user_id,
        sub_agent_id=sub_agent_id,
        name=name,
        job_type=job_type,
        schedule_kind=schedule_kind,
        interval_seconds=interval_seconds,
        cel_expr=cel_expr,
        llm_condition=llm_condition,
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


async def seed_job(
    pg_session: AsyncSession,
    suffix: str,
    *,
    schedule_kind: str = "interval",
    next_run_at: str = "NOW() + INTERVAL '1 hour'",
    enabled: str = "true",
    consecutive_failures: int = 0,
    paused_reason: str | None = None,
    retry_at: str | None = None,
) -> int:
    """Insert a user and one scheduled job; return the job id.

    SQL-expression arguments (*next_run_at*, *retry_at*, *enabled*) are spliced in
    verbatim so tests can say ``NOW() - INTERVAL '1 minute'``. *suffix* keeps the
    user unique across tests sharing a database.
    """
    user_id = f"sched-user-{suffix}"
    await pg_session.execute(
        text(
            "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) "
            "VALUES (:id, :sub, :email, 'S', 'T', false, 'member', 'active')"
        ),
        {"id": user_id, "sub": f"sched-sub-{suffix}", "email": f"sched-{suffix}@test.com"},
    )
    # A watch job: 'task' carries a check constraint requiring a sub-agent, and the job
    # type is irrelevant to what these tests exercise. scheduled_jobs_schedule_config
    # requires exactly the fields its schedule_kind uses, hence the split below.
    if schedule_kind == "once":
        interval_seconds, run_at = "NULL", "NOW() - INTERVAL '1 hour'"
    else:
        interval_seconds, run_at = "3600", "NULL"
    result = await pg_session.execute(
        text(f"""
            INSERT INTO scheduled_jobs
                (user_id, name, job_type, schedule_kind, interval_seconds, run_at, next_run_at,
                 enabled, max_failures, consecutive_failures, destroy_after_trigger,
                 paused_reason, retry_at, check_tool, cel_expr)
            VALUES
                (:uid, 'Seeded Job', 'watch', '{schedule_kind}', {interval_seconds}, {run_at},
                 {next_run_at}, {enabled}, 3, :cf, true, :paused_reason, {retry_at or "NULL"},
                 'ping_tool', 'result != null')
            RETURNING id
        """),
        {"uid": user_id, "cf": consecutive_failures, "paused_reason": paused_reason},
    )
    return result.mappings().first()["id"]


async def seed_run(
    pg_session: AsyncSession,
    job_id: int,
    *,
    status: str = "running",
    started_at: str = "NOW()",
    last_seen_at: str | None = "NOW()",
    completed_at: str | None = None,
    trigger: str = "scheduled",
    notice_due_at: str | None = None,
) -> int:
    """Insert one run row for *job_id*; return the run id. Time arguments are SQL expressions."""
    result = await pg_session.execute(
        text(f"""
            INSERT INTO scheduled_job_runs
                (job_id, status, started_at, last_seen_at, completed_at, trigger, notice_due_at)
            VALUES
                (:job_id, :status, {started_at}, {last_seen_at or "NULL"}, {completed_at or "NULL"},
                 :trigger, {notice_due_at or "NULL"})
            RETURNING id
        """),
        {"job_id": job_id, "status": status, "trigger": trigger},
    )
    return result.mappings().first()["id"]


async def run_status(pg_session: AsyncSession, run_id: int) -> str:
    r = await pg_session.execute(text("SELECT status FROM scheduled_job_runs WHERE id = :id"), {"id": run_id})
    return r.scalar_one()


async def job_retry_at(pg_session: AsyncSession, job_id: int) -> datetime | None:
    r = await pg_session.execute(text("SELECT retry_at FROM scheduled_jobs WHERE id = :id"), {"id": job_id})
    return r.scalar_one()
