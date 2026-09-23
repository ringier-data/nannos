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
    owner_user_id: str | None = None,
    definition_id: int | None = None,
    subscriber_count: int = 1,
    effective_permission: str = "owner",
    job_type: JobType = JobType.TASK,
    sub_agent_id: int | None = 42,
    schedule_kind: ScheduleKind = ScheduleKind.INTERVAL,
    interval_seconds: int | None = 3600,
    cron_expr: str | None = None,
    timezone_name: str | None = None,
    cel_expr: str | None = None,
    llm_condition: str | None = None,
    destroy_after_trigger: bool = True,
    max_failures: int = 3,
    consecutive_failures: int = 0,
    delivery_channel_id: int | None = None,
    name: str = "Test Job",
    owner_email: str | None = None,
    activated_by: str = "user",
    prompt: str | None = "Do something",
    notification_message: str | None = None,
) -> ScheduledJob:
    """An enabled interval task job due in an hour, unless told otherwise.

    The job VIEW (ADR-0010): ``id`` is the subscription id, and unless told otherwise
    the definition carries the same id and the subscriber owns it — the shape every
    pre-split job migrated to.
    """
    now = datetime.now(timezone.utc)
    return ScheduledJob(
        id=job_id,
        user_id=user_id,
        definition_id=definition_id if definition_id is not None else job_id,
        owner_user_id=owner_user_id or user_id,
        owner_email=owner_email,
        activated_by=activated_by,
        effective_permission=effective_permission,  # type: ignore[arg-type]
        subscriber_count=subscriber_count,
        sub_agent_id=sub_agent_id,
        name=name,
        job_type=job_type,
        schedule_kind=schedule_kind,
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone_name,
        cel_expr=cel_expr,
        llm_condition=llm_condition,
        next_run_at=now + timedelta(hours=1),
        prompt=prompt,
        notification_message=notification_message,
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
    # type is irrelevant to what these tests exercise. The definition holds the schedule
    # (its defaults); the subscription inherits it and carries the bookkeeping. The two
    # rows deliberately get DIFFERENT ids (migrated jobs share theirs, fresh ones do not),
    # so a query that confused the two would fail here. The returned id is the
    # subscription's — the job id.
    if schedule_kind == "once":
        interval_seconds, run_at = "NULL", "NOW() - INTERVAL '1 hour'"
    else:
        interval_seconds, run_at = "3600", "NULL"
    result = await pg_session.execute(
        text(f"""
            INSERT INTO scheduled_job_definitions
                (owner_user_id, name, job_type, schedule_kind, interval_seconds, run_at,
                 max_failures, destroy_after_trigger, trigger_policy, check_tool, cel_expr)
            VALUES
                (:uid, 'Seeded Job', 'watch', '{schedule_kind}', {interval_seconds}, {run_at},
                 3, true, 'fixed', 'ping_tool', 'result != null')
            RETURNING id
        """),
        {"uid": user_id},
    )
    definition_id = result.mappings().first()["id"]
    result = await pg_session.execute(
        text(f"""
            INSERT INTO scheduled_job_subscriptions
                (id, definition_id, user_id, next_run_at, enabled, consecutive_failures,
                 paused_reason, retry_at)
            VALUES
                (:id, :definition_id, :uid, {next_run_at}, {enabled}, :cf, :paused_reason, {retry_at or "NULL"})
            RETURNING id
        """),
        {
            "id": definition_id + 1000,
            "definition_id": definition_id,
            "uid": user_id,
            "cf": consecutive_failures,
            "paused_reason": paused_reason,
        },
    )
    job_id = result.mappings().first()["id"]
    # Keep the subscription sequence ahead of the ids we placed by hand.
    await pg_session.execute(
        text("SELECT setval(pg_get_serial_sequence('scheduled_job_subscriptions', 'id'), "
             "GREATEST((SELECT MAX(id) FROM scheduled_job_subscriptions), 1))")
    )
    return job_id


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
                (subscription_id, status, started_at, last_seen_at, completed_at, trigger, notice_due_at)
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
    r = await pg_session.execute(
        text("SELECT retry_at FROM scheduled_job_subscriptions WHERE id = :id"), {"id": job_id}
    )
    return r.scalar_one()
