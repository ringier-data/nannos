"""Repository for scheduled jobs: definitions, subscriptions, and their execution run history.

A scheduled job is two rows (ADR-0010). The DEFINITION is what the job is — owned by one
user, shared to groups with read/write like a sub-agent. A SUBSCRIPTION is one user's
activation of it — enabled state, the trigger in force, delivery target, run
bookkeeping — and every run is a subscription's run under that subscriber's identity.
The ``ScheduledJob`` model this repository hands out is the JOB VIEW: a subscription
with its definition folded in, keyed by the subscription id, which is what everything
outside this module has always called the job id.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from croniter import croniter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..authorization import GROUP_ROLE_CAPABILITIES
from ..models.audit import AuditAction, AuditEntityType
from ..models.scheduled_job import (
    ConditionEvaluation,
    JobRunStatus,
    JobType,
    RunTrigger,
    ScheduledJob,
    ScheduledJobRun,
    ScheduleKind,
    SharedJobDefinition,
    TriggerDefaults,
    TriggerPolicy,
)
from ..models.user import User
from ..utils.timezones import resolve_timezone
from .base import AuditedRepository

logger = logging.getLogger(__name__)


def _roles_with(action: str) -> str:
    """SQL list of group roles whose capabilities include *action* on scheduled_jobs —
    derived from GROUP_ROLE_CAPABILITIES so the three permission fragments below and
    the capability table cannot drift apart."""
    roles = sorted(r for r, caps in GROUP_ROLE_CAPABILITIES.items() if action in caps.get("scheduled_jobs", set()))
    return ", ".join(f"'{r}'" for r in roles)


_WRITE_ROLES = _roles_with("write")
_READ_ROLES = _roles_with("read")

#: One grant a viewer holds through a LIVE group: the grant row, the membership, and the
#: group itself (a soft-deleted group grants nothing). Parameterised on the viewer column.
_GRANT_JOIN = """
    FROM scheduled_job_definition_permissions p
    JOIN user_group_members m ON m.user_group_id = p.user_group_id
    JOIN user_groups ug ON ug.id = p.user_group_id AND ug.deleted_at IS NULL
"""

#: The job view. Trigger columns carry the trigger IN FORCE: the subscription's override
#: when it has one (``s.schedule_kind IS NOT NULL``), else the definition's defaults. The
#: effective timezone is the subscription's, else the definition's, else the subscriber's
#: settings timezone — so a definition that names no zone reads as local time for every
#: subscriber, and one that does pins it for all. Everything under ``d.`` is live for
#: every subscription on its next run; nothing is copied.
#:
#: ``effective_permission`` is computed for the SUBSCRIBER (``s.user_id``), which is the
#: viewer everywhere this view is served. ``write`` needs the group's grant AND a group
#: role that carries write — the same intersection ``check_action_allowed`` applies for
#: sub-agents, spelled out in SQL so a listing is one query.
_JOB_VIEW_SELECT = """
    SELECT s.id,
           s.definition_id,
           s.user_id,
           d.owner_user_id,
           ou.email AS owner_email,
           d.sub_agent_id,
           d.name,
           d.job_type,
           COALESCE(s.schedule_kind, d.schedule_kind)                                   AS schedule_kind,
           CASE WHEN s.schedule_kind IS NULL THEN d.cron_expr        ELSE s.cron_expr        END AS cron_expr,
           CASE WHEN s.schedule_kind IS NULL THEN d.interval_seconds ELSE s.interval_seconds END AS interval_seconds,
           CASE WHEN s.schedule_kind IS NULL THEN d.run_at           ELSE s.run_at           END AS run_at,
           COALESCE(s.timezone, d.timezone, NULLIF(us.timezone, ''))                    AS timezone,
           (s.schedule_kind IS NULL)                                                    AS trigger_inherited,
           s.timezone                                                                   AS timezone_override,
           d.schedule_kind    AS default_schedule_kind,
           d.cron_expr        AS default_cron_expr,
           d.interval_seconds AS default_interval_seconds,
           d.run_at           AS default_run_at,
           d.timezone         AS default_timezone,
           d.trigger_policy,
           s.next_run_at, s.last_run_at, s.retry_at,
           d.prompt, d.notification_message,
           d.check_tool, d.check_args, d.check_args_exprs, d.cel_expr, d.llm_condition,
           d.destroy_after_trigger,
           s.last_check_result,
           s.delivery_channel_id,
           d.voice_call,
           s.enabled, d.max_failures, s.consecutive_failures, s.paused_reason,
           d.revision, d.is_public, d.suspended_at, d.suspended_by_user_id, d.suspended_reason,
           s.activated_by, s.activated_by_groups,
           (SELECT COUNT(*) FROM scheduled_job_subscriptions o
             WHERE o.definition_id = d.id AND o.deleted_at IS NULL)                     AS subscriber_count,
           CASE
               WHEN d.owner_user_id = s.user_id THEN 'owner'
               WHEN EXISTS (
                   SELECT 1 """ + _GRANT_JOIN + """
                   WHERE p.definition_id = d.id
                     AND m.user_id = s.user_id
                     AND 'write' = ANY(p.permissions)
                     AND m.group_role IN (""" + _WRITE_ROLES + """)
               ) THEN 'write'
               -- A subscriber whose grant has since gone still reads as 'read': the
               -- subscription is theirs to manage, whatever they may do to the definition.
               ELSE 'read'
           END                                                                          AS effective_permission,
           s.created_at, s.updated_at, s.deleted_at
    FROM scheduled_job_subscriptions s
    JOIN scheduled_job_definitions d ON d.id = s.definition_id
    LEFT JOIN user_settings us ON us.user_id = s.user_id
    -- Who to name when a run of a shared job arrives ("shared with you by …"). A LEFT
    -- join: a deleted owner leaves the job perfectly runnable for its subscribers.
    LEFT JOIN users ou ON ou.id = d.owner_user_id
"""


def _row_to_scheduled_job(row: Any) -> ScheduledJob:
    """Convert a job-view row mapping to a ScheduledJob model."""
    groups = row.get("activated_by_groups")
    return ScheduledJob(
        id=row["id"],
        user_id=row["user_id"],
        definition_id=row["definition_id"],
        owner_user_id=row["owner_user_id"],
        owner_email=row.get("owner_email"),
        effective_permission=row.get("effective_permission") or "owner",
        sub_agent_id=row["sub_agent_id"],
        name=row["name"],
        job_type=JobType(row["job_type"]),
        schedule_kind=ScheduleKind(row["schedule_kind"]),
        cron_expr=row["cron_expr"],
        timezone=row["timezone"],
        interval_seconds=row["interval_seconds"],
        run_at=row["run_at"],
        trigger_inherited=bool(row.get("trigger_inherited", True)),
        timezone_override=row.get("timezone_override"),
        trigger_defaults=TriggerDefaults(
            schedule_kind=ScheduleKind(row["default_schedule_kind"]),
            cron_expr=row.get("default_cron_expr"),
            interval_seconds=row.get("default_interval_seconds"),
            run_at=row.get("default_run_at"),
            timezone=row.get("default_timezone"),
        ),
        trigger_policy=TriggerPolicy(row["trigger_policy"]),
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
        revision=row.get("revision", 1),
        is_public=row.get("is_public", False),
        suspended_at=row.get("suspended_at"),
        suspended_by_user_id=row.get("suspended_by_user_id"),
        suspended_reason=row.get("suspended_reason"),
        activated_by=row.get("activated_by") or "user",
        activated_by_groups=list(groups) if isinstance(groups, list) else None,
        subscriber_count=int(row.get("subscriber_count") or 1),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row.get("deleted_at"),
    )


def _row_to_run(row: Any) -> ScheduledJobRun:
    """Convert a database row mapping to a ScheduledJobRun model."""
    return ScheduledJobRun(
        id=row["id"],
        job_id=row["subscription_id"],
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
        parked_task_id=row.get("parked_task_id"),
        parked_payload=row.get("parked_payload"),
    )


def _row_to_shared_definition(row: Any) -> SharedJobDefinition:
    return SharedJobDefinition(
        id=row["id"],
        name=row["name"],
        job_type=JobType(row["job_type"]),
        owner_user_id=row["owner_user_id"],
        owner_email=row.get("owner_email"),
        sub_agent_id=row.get("sub_agent_id"),
        prompt=row.get("prompt"),
        check_tool=row.get("check_tool"),
        trigger_defaults=TriggerDefaults(
            schedule_kind=ScheduleKind(row["schedule_kind"]),
            cron_expr=row.get("cron_expr"),
            interval_seconds=row.get("interval_seconds"),
            run_at=row.get("run_at"),
            timezone=row.get("timezone"),
        ),
        trigger_policy=TriggerPolicy(row["trigger_policy"]),
        is_public=bool(row.get("is_public")),
        suspended_at=row.get("suspended_at"),
        revision=row.get("revision", 1),
        subscriber_count=int(row.get("subscriber_count") or 0),
        effective_permission=row.get("effective_permission") or "read",
        subscription_id=row.get("subscription_id"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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


def first_run_at(
    schedule_kind: ScheduleKind,
    cron_expr: str | None,
    interval_seconds: int | None,
    run_at: datetime | None,
    tz: str | None,
    after: datetime | None = None,
) -> datetime:
    """The first occurrence of a trigger — ``compute_next_run``, with a once-job's
    single occurrence being ``run_at`` itself."""
    nxt = compute_next_run(schedule_kind, cron_expr, interval_seconds, run_at, after=after, tz=tz)
    if nxt is None:
        assert run_at is not None, "run_at required for once schedule"
        return run_at
    return nxt


class _SubscriptionRepository(AuditedRepository):
    """The audited half for subscription rows; held by ScheduledJobRepository."""

    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.SCHEDULED_JOB_SUBSCRIPTION,
            table_name="scheduled_job_subscriptions",
        )


class ScheduledJobRepository(AuditedRepository):
    """Repository for scheduled jobs with claim-based execution and run history.

    Audited writes to the DEFINITION go through the inherited ``create``/``update``/
    ``delete`` (entity ``scheduled_job``, so pre-split audit rows read as rows of the
    job's definition); writes to a SUBSCRIPTION go through ``self._subs`` under their
    own entity type.
    """

    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.SCHEDULED_JOB,
            table_name="scheduled_job_definitions",
        )
        self._subs = _SubscriptionRepository()

    def set_audit_service(self, audit_service: Any) -> None:
        super().set_audit_service(audit_service)
        self._subs.set_audit_service(audit_service)

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------

    async def create_definition(self, db: AsyncSession, actor: User, fields: dict[str, Any]) -> int:
        """Insert a definition. Returns its id."""
        return await self.create(db=db, actor=actor, fields=fields, returning="id")

    async def get_definition(self, db: AsyncSession, definition_id: int) -> dict[str, Any] | None:
        """The raw definition row, or None if deleted/unknown."""
        result = await db.execute(
            text("SELECT * FROM scheduled_job_definitions WHERE id = :id AND deleted_at IS NULL"),
            {"id": definition_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def update_definition(
        self,
        db: AsyncSession,
        actor: User,
        definition_id: int,
        fields: dict[str, Any],
        bump_revision: bool = True,
    ) -> None:
        """Update definition fields with audit logging.

        *bump_revision* stamps the edit: every change to what the job IS bumps
        ``revision`` so a run can say which definition produced it. Bookkeeping
        writes (suspend, publish) pass False.
        """
        if bump_revision:
            # AuditedRepository binds every value as a parameter, so an SQL expression
            # cannot travel through it. Bump in a separate statement instead — the
            # revision is not audited field-by-field; the fields that changed are.
            await db.execute(
                text("UPDATE scheduled_job_definitions SET revision = revision + 1 WHERE id = :id"),
                {"id": definition_id},
            )
        await self.update(db=db, actor=actor, entity_id=definition_id, fields=fields)

    async def delete_definition(self, db: AsyncSession, actor: User, definition_id: int) -> list[str]:
        """Soft-delete a definition and every live subscription of it. Returns the
        subscribers' user ids, so the caller can tell them."""
        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                UPDATE scheduled_job_subscriptions
                SET deleted_at = :now, updated_at = :now
                WHERE definition_id = :id AND deleted_at IS NULL
                RETURNING id, user_id
            """),
            {"id": definition_id, "now": now},
        )
        subscribers = []
        for row in result.mappings().all():
            subscribers.append(row["user_id"])
            await self.audit_service.log_action(
                db=db,
                actor=actor,
                entity_type=AuditEntityType.SCHEDULED_JOB_SUBSCRIPTION,
                entity_id=str(row["id"]),
                action=AuditAction.DELETE,
                changes={"soft_delete": True, "cascade_from_definition": definition_id, "user_id": row["user_id"]},
            )
        await self.delete(db=db, actor=actor, entity_id=definition_id)
        return subscribers

    async def set_suspended(
        self,
        db: AsyncSession,
        actor: User,
        definition_id: int,
        suspended: bool,
        reason: str | None = None,
    ) -> None:
        """Suspend or lift the suspension of a definition. Nobody's ``enabled`` changes."""
        now = datetime.now(timezone.utc)
        fields: dict[str, Any] = (
            {"suspended_at": now, "suspended_by_user_id": actor.id, "suspended_reason": reason, "updated_at": now}
            if suspended
            else {"suspended_at": None, "suspended_by_user_id": None, "suspended_reason": None, "updated_at": now}
        )
        await self.update(
            db=db,
            actor=actor,
            entity_id=definition_id,
            fields=fields,
            custom_action=AuditAction.SUSPEND if suspended else AuditAction.UNSUSPEND,
        )

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def create_subscription(self, db: AsyncSession, actor: User, fields: dict[str, Any]) -> int:
        """Insert one subscription. Returns its id (the user-facing job id)."""
        return await self._subs.create(db=db, actor=actor, fields=fields, returning="id")

    async def update_subscription(
        self, db: AsyncSession, actor: User, subscription_id: int, fields: dict[str, Any]
    ) -> None:
        """Update subscription fields with audit logging."""
        await self._subs.update(db=db, actor=actor, entity_id=subscription_id, fields=fields)

    async def delete_subscription(self, db: AsyncSession, actor: User, subscription_id: int) -> None:
        """Soft-delete one subscription (unsubscribe). The definition is untouched."""
        await self._subs.delete(db=db, actor=actor, entity_id=subscription_id)
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=AuditEntityType.SCHEDULED_JOB_SUBSCRIPTION,
            entity_id=str(subscription_id),
            action=AuditAction.UNSUBSCRIBE,
            changes={},
        )

    async def get_job(self, db: AsyncSession, subscription_id: int) -> ScheduledJob | None:
        """Fetch one job view by subscription id."""
        result = await db.execute(
            text(_JOB_VIEW_SELECT + " WHERE s.id = :id AND s.deleted_at IS NULL AND d.deleted_at IS NULL"),
            {"id": subscription_id},
        )
        row = result.mappings().first()
        return _row_to_scheduled_job(row) if row else None

    async def get_subscription_for(
        self, db: AsyncSession, definition_id: int, user_id: str
    ) -> ScheduledJob | None:
        """The user's live subscription to *definition_id*, as a job view, if any."""
        result = await db.execute(
            text(
                _JOB_VIEW_SELECT
                + """
                WHERE s.definition_id = :definition_id AND s.user_id = :user_id
                  AND s.deleted_at IS NULL AND d.deleted_at IS NULL
                """
            ),
            {"definition_id": definition_id, "user_id": user_id},
        )
        row = result.mappings().first()
        return _row_to_scheduled_job(row) if row else None

    async def list_jobs(self, db: AsyncSession, user_id: str) -> list[ScheduledJob]:
        """All live subscriptions of a user, newest first."""
        result = await db.execute(
            text(
                _JOB_VIEW_SELECT
                + """
                WHERE s.user_id = :user_id AND s.deleted_at IS NULL AND d.deleted_at IS NULL
                ORDER BY s.created_at DESC
                """
            ),
            {"user_id": user_id},
        )
        return [_row_to_scheduled_job(r) for r in result.mappings().all()]

    async def list_subscriptions(self, db: AsyncSession, definition_id: int) -> list[ScheduledJob]:
        """Every live subscription of a definition, as job views."""
        result = await db.execute(
            text(
                _JOB_VIEW_SELECT
                + """
                WHERE s.definition_id = :definition_id AND s.deleted_at IS NULL AND d.deleted_at IS NULL
                ORDER BY s.created_at ASC
                """
            ),
            {"definition_id": definition_id},
        )
        return [_row_to_scheduled_job(r) for r in result.mappings().all()]

    async def user_timezones(self, db: AsyncSession, user_ids: list[str]) -> dict[str, str | None]:
        """Each user's settings timezone (None when unset), for computing their first run."""
        if not user_ids:
            return {}
        result = await db.execute(
            text("SELECT user_id, NULLIF(timezone, '') AS timezone FROM user_settings WHERE user_id = ANY(:ids)"),
            {"ids": user_ids},
        )
        found = {r["user_id"]: r["timezone"] for r in result.mappings().all()}
        return {uid: found.get(uid) for uid in user_ids}

    async def clear_trigger_overrides(
        self, db: AsyncSession, actor: User, definition_id: int, next_runs: dict[int, datetime]
    ) -> list[int]:
        """Reset every override on a definition's subscriptions to inherited.

        *next_runs* maps subscription id → its recomputed first occurrence under the
        defaults (computed by the caller, who knows each subscriber's timezone). Only
        subscriptions that actually carried an override are touched; their ids are
        returned so the caller can notify exactly those subscribers.
        """
        result = await db.execute(
            text("""
                SELECT id FROM scheduled_job_subscriptions
                WHERE definition_id = :definition_id AND deleted_at IS NULL
                  AND (schedule_kind IS NOT NULL OR timezone IS NOT NULL)
            """),
            {"definition_id": definition_id},
        )
        overridden = [r["id"] for r in result.mappings().all()]
        now = datetime.now(timezone.utc)
        for sid in overridden:
            fields: dict[str, Any] = {
                "schedule_kind": None,
                "cron_expr": None,
                "interval_seconds": None,
                "run_at": None,
                "timezone": None,
                "updated_at": now,
            }
            if sid in next_runs:
                fields["next_run_at"] = next_runs[sid]
            await self._subs.update(
                db=db, actor=actor, entity_id=sid, fields=fields, custom_action=AuditAction.RESET_OVERRIDES
            )
        return overridden

    async def bulk_subscribe(
        self,
        db: AsyncSession,
        actor: User,
        definition_id: int,
        activations: list[dict[str, Any]],
        activated_by: str,
        group_id: int | None,
        revoked_reason: str | None = None,
    ) -> list[str]:
        """Create a subscription for every user in *activations* who has none yet.

        Each entry is ``{"user_id", "next_run_at", "delivery_channel_id", "enabled",
        "paused_reason"}`` — the first occurrence is computed by the caller in that user's
        own timezone (and a one-shot whose time has passed arrives disabled). Idempotent on
        the live (definition, user) pair; a user who already subscribes keeps their row
        and, when *group_id* is given, gains it in ``activated_by_groups`` so a later
        leave from that group is accounted for. A kept row that was stopped with
        *revoked_reason* (access withdrawn on an earlier leave) is switched back on: the
        member is back, and their customisation with them. Returns the ids of users whose
        subscription was created or re-enabled — the ones owed the consent notice.
        """
        if not activations:
            return []
        now = datetime.now(timezone.utc)
        created: list[str] = []
        for entry in activations:
            result = await db.execute(
                text("""
                    INSERT INTO scheduled_job_subscriptions
                        (definition_id, user_id, activated_by, activated_by_groups,
                         next_run_at, enabled, delivery_channel_id, created_at, updated_at)
                    VALUES
                        (:definition_id, :user_id, :activated_by, CAST(:groups AS jsonb),
                         :next_run_at, :enabled, :delivery_channel_id, :now, :now)
                    ON CONFLICT (definition_id, user_id) WHERE deleted_at IS NULL DO NOTHING
                    RETURNING id
                """),
                {
                    "definition_id": definition_id,
                    "user_id": entry["user_id"],
                    "activated_by": activated_by,
                    "groups": json.dumps([group_id]) if group_id is not None else None,
                    "next_run_at": entry["next_run_at"],
                    "enabled": entry.get("enabled", True),
                    "delivery_channel_id": entry.get("delivery_channel_id"),
                    "now": now,
                },
            )
            row = result.mappings().first()
            if row is not None:
                created.append(entry["user_id"])
                await self.audit_service.log_action(
                    db=db,
                    actor=actor,
                    entity_type=AuditEntityType.SCHEDULED_JOB_SUBSCRIPTION,
                    entity_id=str(row["id"]),
                    action=AuditAction.SUBSCRIBE,
                    changes={
                        "after": {
                            "definition_id": definition_id,
                            "user_id": entry["user_id"],
                            "activated_by": activated_by,
                            "group_id": group_id,
                            "paused_reason": entry.get("paused_reason"),
                        }
                    },
                )
                if entry.get("paused_reason"):
                    await db.execute(
                        text("UPDATE scheduled_job_subscriptions SET paused_reason = :r WHERE id = :id"),
                        {"r": entry["paused_reason"], "id": row["id"]},
                    )
            elif group_id is not None:
                # Already subscribed: record that this group also stands behind it, and
                # lift a stop that access withdrawal put there — the member is back.
                result = await db.execute(
                    text("""
                        UPDATE scheduled_job_subscriptions
                        SET activated_by_groups = (
                                SELECT jsonb_agg(DISTINCT g) FROM jsonb_array_elements(
                                    COALESCE(activated_by_groups, '[]'::jsonb) || CAST(:group AS jsonb)
                                ) AS g
                            ),
                            enabled       = CASE WHEN paused_reason = :revoked THEN TRUE ELSE enabled END,
                            retry_at      = CASE WHEN paused_reason = :revoked THEN NULL ELSE retry_at END,
                            paused_reason = CASE WHEN paused_reason = :revoked THEN NULL ELSE paused_reason END,
                            updated_at = :now
                        WHERE definition_id = :definition_id AND user_id = :user_id AND deleted_at IS NULL
                        RETURNING id, enabled, (paused_reason IS NULL AND enabled) AS live
                    """),
                    {
                        "definition_id": definition_id,
                        "user_id": entry["user_id"],
                        "group": json.dumps([group_id]),
                        "revoked": revoked_reason,
                        "now": now,
                    },
                )
                row = result.mappings().first()
                if row is not None:
                    await self.audit_service.log_action(
                        db=db,
                        actor=actor,
                        entity_type=AuditEntityType.SCHEDULED_JOB_SUBSCRIPTION,
                        entity_id=str(row["id"]),
                        action=AuditAction.UPDATE,
                        changes={"after": {"activated_by_groups_add": group_id, "enabled": row["enabled"]}},
                    )
                    if revoked_reason is not None and row["live"] and entry.get("_was_revoked"):
                        created.append(entry["user_id"])
        return created

    async def group_backed_subscriptions(
        self, db: AsyncSession, definition_ids: list[int], user_ids: list[str], group_id: int
    ) -> list[dict[str, Any]]:
        """Live subscriptions of *user_ids* to *definition_ids* — with whether *group_id*
        stands behind each (activated by a group default of that group)."""
        if not definition_ids or not user_ids:
            return []
        result = await db.execute(
            text("""
                SELECT id, definition_id, user_id, activated_by,
                       COALESCE(activated_by_groups, '[]'::jsonb) AS groups,
                       (COALESCE(activated_by_groups, '[]'::jsonb) @> CAST(:group AS jsonb)) AS via_group
                FROM scheduled_job_subscriptions
                WHERE definition_id = ANY(:definition_ids) AND user_id = ANY(:user_ids) AND deleted_at IS NULL
            """),
            {"definition_ids": definition_ids, "user_ids": user_ids, "group": json.dumps([group_id])},
        )
        return [dict(r) for r in result.mappings().all()]

    async def remove_group_from_subscription(
        self, db: AsyncSession, actor: User, subscription_id: int, group_id: int
    ) -> None:
        """Drop *group_id* from a subscription's ``activated_by_groups`` — audited, since
        this record decides whether a later leave removes the subscription."""
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=AuditEntityType.SCHEDULED_JOB_SUBSCRIPTION,
            entity_id=str(subscription_id),
            action=AuditAction.UPDATE,
            changes={"after": {"activated_by_groups_remove": group_id}},
        )
        await db.execute(
            text("""
                UPDATE scheduled_job_subscriptions
                SET activated_by_groups = (
                        SELECT COALESCE(jsonb_agg(g), '[]'::jsonb)
                        FROM jsonb_array_elements(COALESCE(activated_by_groups, '[]'::jsonb)) AS g
                        WHERE g <> CAST(:group AS jsonb)
                    ),
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": subscription_id, "group": json.dumps(group_id)},
        )

    # ------------------------------------------------------------------
    # Access: who may read/write a definition
    # ------------------------------------------------------------------

    async def user_permission(
        self, db: AsyncSession, definition_id: int, user_id: str, ignore_group: int | None = None
    ) -> str | None:
        """'owner' | 'write' | 'read' | None for *user_id* on *definition_id*.

        Authorization model: effective = resource permissions ∩ group-role capabilities,
        as for sub-agents. A public definition grants read to everyone. *ignore_group*
        answers "what would they still reach without this group" — for a grant revocation
        or a group deletion that has not been written yet.
        """
        result = await db.execute(
            text("""
                SELECT d.owner_user_id, d.is_public,
                       COALESCE((
                           SELECT array_agg(DISTINCT x) """ + _GRANT_JOIN + """
                           CROSS JOIN LATERAL unnest(p.permissions) AS x
                           WHERE p.definition_id = d.id AND m.user_id = :user_id
                             AND p.user_group_id IS DISTINCT FROM :ignore_group
                             AND (   (x = 'read'  AND m.group_role IN (""" + _READ_ROLES + """))
                                  OR (x = 'write' AND m.group_role IN (""" + _WRITE_ROLES + """)))
                       ), ARRAY[]::text[]) AS grants
                FROM scheduled_job_definitions d
                WHERE d.id = :id AND d.deleted_at IS NULL
            """),
            {"id": definition_id, "user_id": user_id, "ignore_group": ignore_group},
        )
        row = result.mappings().first()
        if row is None:
            return None
        if row["owner_user_id"] == user_id:
            return "owner"
        grants = set(row["grants"] or [])
        if "write" in grants:
            return "write"
        if "read" in grants or row["is_public"]:
            return "read"
        return None

    async def get_permissions(self, db: AsyncSession, definition_id: int) -> list[dict[str, Any]]:
        """Group permissions on a definition, with group names."""
        result = await db.execute(
            text("""
                SELECT p.user_group_id, ug.name AS user_group_name, p.permissions
                FROM scheduled_job_definition_permissions p
                JOIN user_groups ug ON ug.id = p.user_group_id
                WHERE p.definition_id = :id
                ORDER BY ug.name
            """),
            {"id": definition_id},
        )
        return [dict(r) for r in result.mappings().all()]

    async def replace_permissions(
        self,
        db: AsyncSession,
        actor: User,
        definition_id: int,
        group_permissions: list[dict[str, Any]],
    ) -> tuple[set[int], set[int], set[int]]:
        """Replace a definition's group grants. Returns (added, removed, changed) group ids."""
        current = {r["user_group_id"]: set(r["permissions"]) for r in await self.get_permissions(db, definition_id)}
        new = {p["user_group_id"]: set(p["permissions"]) for p in group_permissions}
        added = set(new) - set(current)
        removed = set(current) - set(new)
        changed = {gid for gid in new if gid in current and new[gid] != current[gid]}

        await db.execute(
            text("DELETE FROM scheduled_job_definition_permissions WHERE definition_id = :id"),
            {"id": definition_id},
        )
        for perm in group_permissions:
            await db.execute(
                text("""
                    INSERT INTO scheduled_job_definition_permissions (definition_id, user_group_id, permissions)
                    VALUES (:definition_id, :user_group_id, :permissions)
                """),
                {
                    "definition_id": definition_id,
                    "user_group_id": perm["user_group_id"],
                    "permissions": sorted(perm["permissions"]),
                },
            )
        await self.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.entity_type,
            entity_id=str(definition_id),
            action=AuditAction.PERMISSION_UPDATE,
            changes={
                "before": {"permissions": [{"user_group_id": g, "permissions": sorted(p)} for g, p in current.items()]},
                "after": {"permissions": group_permissions},
            },
        )
        return added, removed, changed

    async def group_member_ids(self, db: AsyncSession, group_ids: list[int]) -> list[str]:
        """Distinct active user ids across *group_ids*."""
        if not group_ids:
            return []
        result = await db.execute(
            text("""
                SELECT DISTINCT m.user_id
                FROM user_group_members m
                JOIN users u ON u.id = m.user_id AND u.deleted_at IS NULL
                JOIN user_groups ug ON ug.id = m.user_group_id AND ug.deleted_at IS NULL
                WHERE m.user_group_id = ANY(:ids)
            """),
            {"ids": group_ids},
        )
        return [r["user_id"] for r in result.mappings().all()]

    async def list_available_definitions(self, db: AsyncSession, user_id: str) -> list[SharedJobDefinition]:
        """Definitions *user_id* can read — owned, public, or shared to one of their
        groups — with their live subscription id when they have one."""
        result = await db.execute(
            text("""
                SELECT d.*, u.email AS owner_email,
                       (SELECT COUNT(*) FROM scheduled_job_subscriptions o
                         WHERE o.definition_id = d.id AND o.deleted_at IS NULL) AS subscriber_count,
                       (SELECT s.id FROM scheduled_job_subscriptions s
                         WHERE s.definition_id = d.id AND s.user_id = :user_id AND s.deleted_at IS NULL
                         LIMIT 1) AS subscription_id,
                       CASE
                           WHEN d.owner_user_id = :user_id THEN 'owner'
                           WHEN EXISTS (
                               SELECT 1 """ + _GRANT_JOIN + """
                               WHERE p.definition_id = d.id AND m.user_id = :user_id
                                 AND 'write' = ANY(p.permissions) AND m.group_role IN (""" + _WRITE_ROLES + """)
                           ) THEN 'write'
                           ELSE 'read'
                       END AS effective_permission
                FROM scheduled_job_definitions d
                JOIN users u ON u.id = d.owner_user_id
                WHERE d.deleted_at IS NULL
                  AND (
                        d.owner_user_id = :user_id
                     OR d.is_public = TRUE
                     -- The same predicate user_permission applies, so nothing is listed
                     -- that subscribe would then refuse.
                     OR EXISTS (
                            SELECT 1 """ + _GRANT_JOIN + """
                            WHERE p.definition_id = d.id AND m.user_id = :user_id
                              AND (   ('read'  = ANY(p.permissions) AND m.group_role IN (""" + _READ_ROLES + """))
                                   OR ('write' = ANY(p.permissions) AND m.group_role IN (""" + _WRITE_ROLES + """)))
                        )
                  )
                ORDER BY d.updated_at DESC
            """),
            {"user_id": user_id},
        )
        return [_row_to_shared_definition(r) for r in result.mappings().all()]

    async def list_group_definitions(self, db: AsyncSession, group_id: int) -> list[dict[str, Any]]:
        """Definitions shared to *group_id*, with whether each is one of its defaults."""
        result = await db.execute(
            text("""
                SELECT d.id, d.name, d.job_type, d.owner_user_id,
                       (d.suspended_at IS NOT NULL) AS suspended,
                       (dj.definition_id IS NOT NULL) AS is_default
                FROM scheduled_job_definition_permissions p
                JOIN scheduled_job_definitions d ON d.id = p.definition_id
                LEFT JOIN user_group_default_jobs dj
                       ON dj.definition_id = d.id AND dj.user_group_id = :group_id
                WHERE p.user_group_id = :group_id AND d.deleted_at IS NULL
                ORDER BY is_default DESC, d.name ASC
            """),
            {"group_id": group_id},
        )
        return [dict(r) for r in result.mappings().all()]

    async def group_has_grant(self, db: AsyncSession, definition_id: int, group_id: int) -> bool:
        result = await db.execute(
            text("""
                SELECT 1 FROM scheduled_job_definition_permissions
                WHERE definition_id = :definition_id AND user_group_id = :group_id
            """),
            {"definition_id": definition_id, "group_id": group_id},
        )
        return result.first() is not None

    # ------------------------------------------------------------------
    # Group defaults
    # ------------------------------------------------------------------

    async def get_group_default_definition_ids(self, db: AsyncSession, group_id: int) -> list[int]:
        result = await db.execute(
            text("""
                SELECT dj.definition_id
                FROM user_group_default_jobs dj
                JOIN scheduled_job_definitions d ON d.id = dj.definition_id AND d.deleted_at IS NULL
                WHERE dj.user_group_id = :group_id
                ORDER BY dj.created_at
            """),
            {"group_id": group_id},
        )
        return [r["definition_id"] for r in result.mappings().all()]

    async def add_group_default(self, db: AsyncSession, group_id: int, definition_id: int, actor: User) -> bool:
        """Record a default (audited on the definition: it subscribes every current and
        future member of the group). Returns False if it already was one."""
        result = await db.execute(
            text("""
                INSERT INTO user_group_default_jobs (user_group_id, definition_id, created_by_user_id)
                VALUES (:group_id, :definition_id, :user_id)
                ON CONFLICT (user_group_id, definition_id) DO NOTHING
                RETURNING id
            """),
            {"group_id": group_id, "definition_id": definition_id, "user_id": actor.id},
        )
        added = result.first() is not None
        if added:
            await self.audit_service.log_action(
                db=db,
                actor=actor,
                entity_type=self.entity_type,
                entity_id=str(definition_id),
                action=AuditAction.ASSIGN,
                changes={"after": {"group_default": group_id}},
            )
        return added

    async def remove_group_default(self, db: AsyncSession, group_id: int, definition_id: int, actor: User) -> bool:
        result = await db.execute(
            text("DELETE FROM user_group_default_jobs WHERE user_group_id = :group_id AND definition_id = :definition_id"),
            {"group_id": group_id, "definition_id": definition_id},
        )
        removed = result.rowcount > 0
        if removed:
            await self.audit_service.log_action(
                db=db,
                actor=actor,
                entity_type=self.entity_type,
                entity_id=str(definition_id),
                action=AuditAction.UNASSIGN,
                changes={"before": {"group_default": group_id}},
            )
        return removed

    async def group_default_ids_for_group(self, db: AsyncSession, group_id: int) -> list[int]:
        """Every default row of *group_id*, deleted definitions included — for cleaning up
        after a group is (soft-)deleted, which fires no FK cascade."""
        result = await db.execute(
            text("SELECT definition_id FROM user_group_default_jobs WHERE user_group_id = :g"), {"g": group_id}
        )
        return [r["definition_id"] for r in result.mappings().all()]

    async def owner_channel_id(self, db: AsyncSession, definition_id: int, owner_user_id: str) -> int | None:
        """The owner's subscription's delivery channel — what a new subscription inherits."""
        result = await db.execute(
            text("""
                SELECT delivery_channel_id FROM scheduled_job_subscriptions
                WHERE definition_id = :d AND user_id = :u AND deleted_at IS NULL
            """),
            {"d": definition_id, "u": owner_user_id},
        )
        return result.scalar_one_or_none()

    async def agent_is_automated(self, db: AsyncSession, sub_agent_id: int) -> bool:
        """Whether *sub_agent_id* is an inline ``automated`` agent — one that belongs to the
        definition naming it and travels with it."""
        result = await db.execute(
            text("SELECT type = 'automated' FROM sub_agents WHERE id = :id AND deleted_at IS NULL"),
            {"id": sub_agent_id},
        )
        return bool(result.scalar_one_or_none())

    async def subscriber_can_run_agent(self, db: AsyncSession, user_id: str, sub_agent_id: int) -> bool:
        """Whether *user_id* may run *sub_agent_id* right now — one EXISTS, evaluated on
        every dispatch.

        Reproduces the access arms of ``SubAgentService.get_accessible_sub_agents`` (owner,
        public, group grant, embed activation) plus ADR-0010's own: an inline ``automated``
        agent is reachable for anyone holding a live subscription to a definition that names
        it. Kept here, not delegated to that service, because the listing hydrates every
        accessible agent's whole config to answer a boolean.
        """
        result = await db.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM sub_agents sa
                    WHERE sa.id = :agent_id AND sa.deleted_at IS NULL
                      AND (
                            sa.owner_user_id = :user_id
                         OR sa.is_public = TRUE
                         OR EXISTS (
                                SELECT 1 FROM sub_agent_permissions sap
                                JOIN user_group_members m ON m.user_group_id = sap.user_group_id
                                JOIN user_groups ug ON ug.id = sap.user_group_id AND ug.deleted_at IS NULL
                                WHERE sap.sub_agent_id = sa.id AND m.user_id = :user_id
                            )
                         OR EXISTS (
                                SELECT 1 FROM user_sub_agent_activations usa
                                WHERE usa.sub_agent_id = sa.id AND usa.user_id = :user_id
                                  AND usa.activated_by = 'embed'
                            )
                         OR (sa.type = 'automated' AND EXISTS (
                                SELECT 1 FROM scheduled_job_subscriptions sjs
                                JOIN scheduled_job_definitions sjd ON sjd.id = sjs.definition_id
                                WHERE sjd.sub_agent_id = sa.id AND sjs.user_id = :user_id
                                  AND sjs.deleted_at IS NULL AND sjd.deleted_at IS NULL
                            ))
                      )
                )
            """),
            {"agent_id": sub_agent_id, "user_id": user_id},
        )
        return bool(result.scalar_one())

    async def definitions_shared_to_group(self, db: AsyncSession, group_id: int) -> list[int]:
        """Ids of live definitions with any grant to *group_id*."""
        result = await db.execute(
            text("""
                SELECT p.definition_id FROM scheduled_job_definition_permissions p
                JOIN scheduled_job_definitions d ON d.id = p.definition_id AND d.deleted_at IS NULL
                JOIN user_groups ug ON ug.id = p.user_group_id AND ug.deleted_at IS NULL
                WHERE p.user_group_id = :group_id
            """),
            {"group_id": group_id},
        )
        return [r["definition_id"] for r in result.mappings().all()]

    # ------------------------------------------------------------------
    # Dispatch: claim and complete
    # ------------------------------------------------------------------

    async def claim_due_jobs(self, db: AsyncSession, limit: int = 10) -> list[ClaimedJob]:
        """Claim up to *limit* due subscriptions using SELECT … FOR UPDATE SKIP LOCKED.

        Marks each claimed subscription by setting last_run_at = NOW() to prevent
        double-processing in a multi-instance deployment. The caller is responsible
        for updating next_run_at once execution completes.

        Only SUBSCRIPTIONS are claimed — a definition never runs — and a suspended or
        deleted definition holds every subscription of it out of the claim, whatever
        each member's own ``enabled`` says.

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

        A subscription with a run still ``running`` is not claimable on either branch.
        The schedule does not advance until a run completes, so without this a process
        restart would re-claim it through a stale ``next_run_at`` while its stranded
        run waits for the healer, and the interruption would then earn a retry on top.

        A run parked on its subscriber holds the schedule the same way — per
        subscription, so another subscriber's run of the same definition is
        unaffected (ADR-0009 under ADR-0010). What holds it is an *answerable* park,
        ``parked_task_id IS NOT NULL``, cleared the moment somebody answers; the status
        alone stays ``auth_required`` for good.

        The two are separate ``NOT EXISTS`` clauses rather than one ``status IN (…)``
        because a partial index cannot serve an ``IN`` predicate.

        ``retry_at`` is cleared on claim, so an attempt is handed out once even if
        several schedulers tick together.
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                WITH due AS (
                    SELECT s.id,
                           (s.retry_at IS NOT NULL AND s.retry_at <= :now) AS via_retry
                    FROM scheduled_job_subscriptions s
                    JOIN scheduled_job_definitions d ON d.id = s.definition_id
                    WHERE s.deleted_at IS NULL
                      AND d.deleted_at IS NULL
                      AND d.suspended_at IS NULL
                      AND (
                            (s.enabled = TRUE AND s.next_run_at <= :now)
                         OR (s.retry_at IS NOT NULL AND s.retry_at <= :now AND s.paused_reason IS NULL)
                      )
                      AND NOT EXISTS (
                            SELECT 1 FROM scheduled_job_runs r
                            WHERE r.subscription_id = s.id
                              AND r.status = 'running'
                      )
                      AND NOT EXISTS (
                            SELECT 1 FROM scheduled_job_runs r
                            WHERE r.subscription_id = s.id
                              AND r.status = 'auth_required'
                              AND r.parked_task_id IS NOT NULL
                      )
                    ORDER BY COALESCE(s.retry_at, s.next_run_at) ASC
                    LIMIT :limit
                    FOR UPDATE OF s SKIP LOCKED
                )
                SELECT v.*, due.via_retry
                FROM due
                JOIN ("""
                + _JOB_VIEW_SELECT
                + """) v ON v.id = due.id
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
            text("UPDATE scheduled_job_subscriptions SET last_run_at = :now, retry_at = NULL WHERE id = ANY(:ids)"),
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
        subscription_id: int,
        status: JobRunStatus,
        next_run_at: datetime | None,
        last_check_result: dict[str, Any] | None = None,
        paused_reason: str | None = None,
        retry_at: datetime | None = None,
        leave_schedule: bool = False,
    ) -> tuple[bool, str | None]:
        """Update a subscription after execution: advance schedule, track failures, auto-pause on threshold.

        Returns ``(enabled, paused_reason)`` as this write left them. The caller compares
        against the state it already held to tell "this run stopped the job" from "it was
        already off" — which it needs in order to tell the subscriber, and which it cannot
        learn by re-reading: the auto-pause decision is made inside this statement, from
        ``consecutive_failures`` against the definition's ``max_failures``.

        Takes the run's status rather than a success flag because there are more than
        two outcomes. Only a FAILED run moves ``consecutive_failures`` up and only a
        successful one resets it; INTERRUPTED and AUTH_REQUIRED leave it alone in both
        directions, because neither is evidence about the job. See
        docs/adr/0007-interrupted-runs-get-one-fresh-attempt.md and
        docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.

        An AUTH_REQUIRED run still advances ``next_run_at`` like any other. What stops
        the subscription running is ``claim_due_jobs``, which will not claim one whose
        run is parked — so the schedule stays honest about when the job was due, and
        it catches up with one occurrence once the subscriber answers.

        *leave_schedule* says this run does not own the schedule: ``next_run_at`` and
        ``enabled`` are whatever else has set them. A NULL ``next_run_at`` otherwise
        carries two meanings at once — the COALESCE keeps the column, and the CASE below
        retires the subscription — so a caller that merely has nothing to say about the
        schedule could not say so, and had to echo back a value it read earlier instead.
        That read is what made a resumed run overwrite a concurrent schedule edit.

        *retry_at* schedules the one fresh attempt an interruption earns. It is
        only ever written, never cleared here: runs of one subscription can complete
        out of order, and a completion that wiped the marker would silently cancel an
        attempt that was earned. The claim consumes it; pause and resume clear it.
        """
        now = datetime.now(timezone.utc)
        failed = status == JobRunStatus.FAILED
        success = status in (JobRunStatus.SUCCESS, JobRunStatus.CONDITION_NOT_MET)

        result = await db.execute(
            text("""
                UPDATE scheduled_job_subscriptions s
                SET
                    consecutive_failures = CASE
                        WHEN :failed  THEN s.consecutive_failures + 1
                        WHEN :success THEN 0
                        ELSE s.consecutive_failures
                    END,
                    last_run_at          = :last_run_at,
                    next_run_at          = COALESCE(:next_run_at, s.next_run_at),
                    retry_at             = COALESCE(CAST(:retry_at AS timestamptz), s.retry_at),
                    enabled              = CASE
                        WHEN :next_run_at IS NULL AND NOT :leave_schedule                   THEN FALSE
                        WHEN :failed AND (s.consecutive_failures + 1) >= d.max_failures      THEN FALSE
                        ELSE s.enabled
                    END,
                    paused_reason        = CASE
                        WHEN :failed AND (s.consecutive_failures + 1) >= d.max_failures
                            THEN 'Auto-paused after ' || d.max_failures || ' consecutive failures'
                        WHEN CAST(:paused_reason AS text) IS NOT NULL THEN CAST(:paused_reason AS text)
                        ELSE s.paused_reason
                    END,
                    last_check_result    = COALESCE(CAST(:last_check_result AS jsonb), s.last_check_result),
                    updated_at           = :now
                FROM scheduled_job_definitions d
                WHERE s.id = :subscription_id AND d.id = s.definition_id
                RETURNING s.enabled, s.paused_reason
            """),
            {
                "subscription_id": subscription_id,
                "failed": failed,
                "success": success,
                "last_run_at": now,
                "next_run_at": next_run_at,
                "leave_schedule": leave_schedule,
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
        row = result.mappings().first()
        return (bool(row["enabled"]), row["paused_reason"]) if row else (True, None)

    async def disable_subscription(
        self, db: AsyncSession, subscription_id: int, reason: str
    ) -> None:
        """A system stop of one subscription (no actor): destroy-after-trigger, an
        agent the subscriber can no longer reach. Writes the reason, as every
        deliberate stop must, so the retry branch of the claim leaves it alone."""
        await db.execute(
            text("""
                UPDATE scheduled_job_subscriptions
                SET enabled = FALSE, paused_reason = :reason, retry_at = NULL, updated_at = :now
                WHERE id = :id
            """),
            {"id": subscription_id, "reason": reason, "now": datetime.now(timezone.utc)},
        )

    async def create_run(
        self,
        db: AsyncSession,
        subscription_id: int,
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
                INSERT INTO scheduled_job_runs (subscription_id, started_at, status, last_seen_at, trigger)
                VALUES (:subscription_id, NOW(), 'running', NOW(), :trigger)
                RETURNING id
            """),
            {"subscription_id": subscription_id, "trigger": trigger.value},
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
        """Claim owed notices that are due; return ``{run_id, subscription_id}`` for each.

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
                                WHERE newer.subscription_id = r.subscription_id
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
                    RETURNING r.id, r.subscription_id
                )
                SELECT 'abandoned' AS outcome, id, NULL::bigint AS subscription_id, too_old FROM abandoned
                UNION ALL
                SELECT 'claimed', id, subscription_id, NULL FROM claimed
            """),
            {"give_up_before": give_up_before, "limit": limit, "next_attempt_at": next_attempt_at},
        )
        claimed: list[dict[str, Any]] = []
        for row in result.mappings().all():
            if row["outcome"] == "claimed":
                claimed.append({"run_id": row["id"], "subscription_id": row["subscription_id"]})
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

        A ``resumed`` run earns the attempt too (ADR-0009 decision 7). Nobody is present
        — the click that started it is long gone — and the ask it answered has already
        been consumed, so without this the job would stop for good on an authorization
        that actually succeeded. The fresh attempt is cheap by then: the credential is
        stored at the gateway, so an ordinary occurrence no longer blocks on it and
        recovers the work the resume was carrying. This is also what makes the window
        between claiming the ask and dispatching the resume survivable — the run row is
        written with the claim, so a process that dies in between leaves a stale
        ``running`` row here rather than nothing at all.
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
                    RETURNING id, subscription_id, trigger
                ), retried AS (
                    UPDATE scheduled_job_subscriptions j
                    SET retry_at   = :retry_at,
                        updated_at = NOW()
                    FROM stale
                    WHERE j.id = stale.subscription_id
                      AND stale.trigger IN ('scheduled', 'resumed')
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
        parked_task_id: str | None = None,
        parked_payload: dict[str, Any] | None = None,
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

        *parked_task_id* and *parked_payload* are the parked agent-runner task and the ask
        delivered with it, on an AUTH_REQUIRED run. Written here rather than by a later
        update for the same reason as the notice: the run and the reason it stopped are
        one fact, and a run recorded as parked with no way to reach the task would be a
        job stopped with no way to restart it.
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
                    notice_due_at    = COALESCE(CAST(:notice_due_at AS timestamptz), notice_due_at),
                    parked_task_id     = COALESCE(:parked_task_id, parked_task_id),
                    parked_payload     = COALESCE(CAST(:parked_payload AS jsonb), parked_payload)
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
                "parked_task_id": parked_task_id,
                "parked_payload": json.dumps(parked_payload) if parked_payload is not None else None,
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

    async def answerable_parked_run(self, db: AsyncSession, subscription_id: int) -> ScheduledJobRun | None:
        """The run of *subscription_id* still waiting on its owner, if any.

        ``parked_task_id`` rather than status is what makes a run answerable: the status
        stays ``auth_required`` for good, because it is a true record of how that
        occurrence ended, while the task id is cleared the moment somebody answers.
        """
        result = await db.execute(
            text("""
                SELECT * FROM scheduled_job_runs
                WHERE subscription_id = :subscription_id
                  AND status = 'auth_required'
                  AND parked_task_id IS NOT NULL
                ORDER BY started_at DESC
                LIMIT 1
            """),
            {"subscription_id": subscription_id},
        )
        row = result.mappings().first()
        return _row_to_run(row) if row is not None else None

    async def clear_parked_task(self, db: AsyncSession, run_id: int) -> bool:
        """Mark a parked run as no longer answerable. Returns whether it still was.

        Called once the owner's answer has been accepted. The run KEEPS its
        ``AUTH_REQUIRED`` status — it is a true record of how that occurrence ended, and
        rewriting it would lose that — so the task id is what says whether there is still
        a question outstanding. Without this the ask never goes away: the console would
        go on offering a card for a task that has since gone terminal, and a second click
        would surface the A2A server's "task is in terminal state" at the user.

        The write is conditional, so two clicks racing produce one resume: the loser sees
        no row updated and is told the run has already been answered.
        """
        result = await db.execute(
            text("""
                UPDATE scheduled_job_runs
                SET parked_task_id = NULL
                WHERE id = :run_id AND parked_task_id IS NOT NULL
            """),
            {"run_id": run_id},
        )
        return result.rowcount > 0

    async def get_run(
        self,
        db: AsyncSession,
        subscription_id: int,
        run_id: int,
    ) -> ScheduledJobRun | None:
        """Fetch a single run of a job by id, regardless of age."""
        result = await db.execute(
            text("""
                SELECT * FROM scheduled_job_runs
                WHERE id = :run_id AND subscription_id = :subscription_id
            """),
            {"run_id": run_id, "subscription_id": subscription_id},
        )
        row = result.mappings().first()
        return _row_to_run(row) if row is not None else None

    async def list_runs(
        self,
        db: AsyncSession,
        subscription_id: int,
        limit: int = 50,
    ) -> list[ScheduledJobRun]:
        """Fetch the most recent runs for a job, newest first."""
        result = await db.execute(
            text("""
                SELECT * FROM scheduled_job_runs
                WHERE subscription_id = :subscription_id
                ORDER BY started_at DESC
                LIMIT :limit
            """),
            {"subscription_id": subscription_id, "limit": limit},
        )
        return [_row_to_run(r) for r in result.mappings().all()]
