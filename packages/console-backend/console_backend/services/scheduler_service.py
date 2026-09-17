"""Scheduler service — scheduled job definitions, subscriptions and sharing.

A scheduled job is a DEFINITION (what it is, owned by one user, shareable to groups)
plus one SUBSCRIPTION per user who runs it (ADR-0010). The service keeps the split
invisible where it can: ``create_job`` writes both, ``get_job``/``list_jobs`` serve the
folded job view, and ``update_job`` routes each field to the side it lives on.
"""

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from console_backend.models.notification import NotificationData, NotificationType
from console_backend.models.sub_agent import SubAgentCreate, SubAgentType
from console_backend.services.sub_agent_service import SubAgentService
from console_backend.services.user_settings_service import UserSettingsService
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.scheduled_job import (
    JobType,
    ScheduledJob,
    ScheduledJobCreate,
    ScheduledJobRun,
    ScheduledJobUpdate,
    ScheduleKind,
    SharedJobDefinition,
    TriggerPolicy,
)
from ..models.user import User
from ..repositories.scheduled_job_repository import ScheduledJobRepository, compute_next_run, first_run_at
from ..utils.timezones import default_timezone_name, resolve_timezone, validate_timezone_name

# Sentinel value to distinguish "no change" from "set to None"
_UNSET: Any = object()

if TYPE_CHECKING:
    from ..repositories.delivery_channel_repository import DeliveryChannelRepository
    from .notification_service import NotificationService

logger = logging.getLogger(__name__)

#: Fields of ScheduledJobUpdate that live on the DEFINITION and need `write`.
_DEFINITION_FIELDS = frozenset(
    {
        "name",
        "prompt",
        "notification_message",
        "sub_agent_id",
        "check_tool",
        "check_args",
        "check_args_exprs",
        "cel_expr",
        "llm_condition",
        "destroy_after_trigger",
        "voice_call",
        "max_failures",
        "trigger_policy",
    }
)
#: The trigger: the only ambiguous group once a definition has other subscribers.
_TRIGGER_FIELDS = frozenset({"schedule_kind", "cron_expr", "interval_seconds", "run_at", "timezone"})
#: Fields that are always the caller's own subscription.
_SUBSCRIPTION_FIELDS = frozenset({"enabled", "delivery_channel_id"})
#: Every field of ScheduledJobUpdate is routed somewhere. A field added to the model
#: without a home here fails at import instead of being silently dropped by PATCH.
assert _DEFINITION_FIELDS | _TRIGGER_FIELDS | _SUBSCRIPTION_FIELDS | {"scope"} == set(
    ScheduledJobUpdate.model_fields
), "ScheduledJobUpdate has a field update_job does not route"

#: What a subscriber whose access was revoked is told on their own (self-made)
#: subscription. Re-granting access lets them enable it again with their customisation.
_ACCESS_REVOKED_REASON = "Access to this shared job was revoked"


class SchedulerAccessError(PermissionError):
    """The caller lacks the permission an operation needs on a definition."""


class SchedulerService:
    """Service for scheduled job definitions, subscriptions and sharing."""

    def __init__(
        self, repository: ScheduledJobRepository | None = None, sub_agent_service: SubAgentService | None = None
    ) -> None:
        self._repo = repository
        self._sub_agent_service = sub_agent_service
        self._delivery_channel_repo: "DeliveryChannelRepository | None" = None
        self._user_settings_service: UserSettingsService | None = None
        self._notification_service: "NotificationService | None" = None

    def set_repository(self, repository: ScheduledJobRepository) -> None:
        self._repo = repository

    def set_sub_agent_service(self, sub_agent_service: SubAgentService) -> None:
        self._sub_agent_service = sub_agent_service

    def set_user_settings_service(self, user_settings_service: UserSettingsService) -> None:
        self._user_settings_service = user_settings_service

    def set_delivery_channel_repository(self, repository: "DeliveryChannelRepository") -> None:
        self._delivery_channel_repo = repository

    def set_notification_service(self, notification_service: "NotificationService") -> None:
        self._notification_service = notification_service

    async def _validate_delivery_channel(self, db: AsyncSession, channel_id: int) -> None:
        """Ensure the referenced delivery channel exists before it reaches the FK constraint.

        Without this, an unknown ``delivery_channel_id`` only fails at the database
        foreign-key constraint and surfaces as an unhandled 500. Raising ValueError
        here lets the router translate it into a clean 400.
        """
        if self._delivery_channel_repo is None:
            raise RuntimeError(
                "DeliveryChannelRepository not injected. Call set_delivery_channel_repository() during initialization."
            )
        channel = await self._delivery_channel_repo.get_channel_by_id(db, channel_id)
        if channel is None:
            raise ValueError(f"Delivery channel {channel_id} not found")

    @property
    def repo(self) -> ScheduledJobRepository:
        if self._repo is None:
            raise RuntimeError("ScheduledJobRepository not injected. Call set_repository() during initialization.")
        return self._repo

    @property
    def sub_agents(self) -> SubAgentService:
        if self._sub_agent_service is None:
            raise RuntimeError("SubAgentService not injected. Call set_sub_agent_service() during initialization.")
        return self._sub_agent_service

    # ------------------------------------------------------------------
    # Timezones
    # ------------------------------------------------------------------

    async def _user_timezone(self, db: AsyncSession, user_id: str) -> str:
        """The user's settings timezone, else the deployment default — validated."""
        if self._user_settings_service is None:
            raise RuntimeError(
                "UserSettingsService not injected. Call set_user_settings_service() during initialization."
            )
        settings = await self._user_settings_service.get_settings(db, user_id)
        tz = settings.timezone or default_timezone_name()
        try:
            validate_timezone_name(tz)
        except ValueError as e:
            # Legacy settings rows predate schema validation, so surface a clean
            # 400 instead of a 500 from deep inside croniter.
            raise ValueError(
                f"Your settings timezone {settings.timezone!r} is not a valid IANA timezone; "
                "fix it in Settings or pass an explicit job timezone."
            ) from e
        return tz

    async def _resolve_timezone(self, db: AsyncSession, requested: str | None, user_id: str) -> str:
        """The timezone a trigger is evaluated in for *user_id*: explicit wins, else theirs."""
        if requested:
            return requested
        return await self._user_timezone(db, user_id)

    @staticmethod
    def _normalize_run_at(run_at: datetime | None, tz: str | None) -> datetime | None:
        """Attach a timezone to a naive run_at.

        The frontend's datetime-local input submits wall-clock strings without an
        offset; storing them unmodified makes Postgres read them as UTC.
        """
        if run_at is not None and run_at.tzinfo is None:
            return run_at.replace(tzinfo=resolve_timezone(tz))
        return run_at

    @staticmethod
    def _same_definition_value(job: ScheduledJob, key: str, value: Any) -> bool:
        """Whether *value* equals what the definition already holds for *key* (JSON fields
        are compared as objects, since they arrive serialised)."""
        current = getattr(job, key, None)
        if key in ("check_args", "check_args_exprs"):
            if value is None or current is None:
                return value is None and current is None
            return json.loads(value) == current
        if key == "trigger_policy":
            return current is not None and current.value == value
        return current == value

    @staticmethod
    def _same_trigger_value(job: ScheduledJob, key: str, value: Any) -> bool:
        current = getattr(job, key)
        if key == "schedule_kind":
            return current == ScheduleKind(value)
        if key == "run_at" and value is not None and current is not None:
            if value.tzinfo is None:
                # A naive echo of a stored instant: compare as the wall-clock the form shows.
                return current.astimezone(resolve_timezone(job.timezone)).replace(tzinfo=None) == value
            return current == value
        return current == value

    @staticmethod
    def _effective_tz(*candidates: str | None) -> str | None:
        """First non-empty timezone of the inheritance chain: subscription, definition, subscriber."""
        for tz in candidates:
            if tz:
                return tz
        return None

    # ------------------------------------------------------------------
    # Sub-agent access
    # ------------------------------------------------------------------

    async def schedulable_sub_agents(self, db: AsyncSession, user_id: str) -> list:
        """The sub-agents a scheduled job may run on this user's behalf.

        One definition on purpose. Anything that offers a choice of sub-agent — the
        picker, the AI fill — has to offer exactly this set, because this is what
        create_job validates against. An offer that is wider produces a job the user
        cannot save; one that is narrower hides agents they could have used.

        Note it is deliberately not admin-aware: a scheduled job runs as its owner, so
        being an administrator does not widen what a job of theirs may invoke.
        """
        return await self.sub_agents.get_accessible_sub_agents(db, user_id)

    async def _assert_agent_accessible(self, db: AsyncSession, user_id: str, sub_agent_id: int, verb: str) -> None:
        accessible = await self.schedulable_sub_agents(db, user_id)
        if not any(sa.id == sub_agent_id for sa in accessible):
            raise ValueError(f"Access denied: You do not have permission to {verb} sub-agent {sub_agent_id}")

    async def subscriber_can_run_agent(self, db: AsyncSession, user_id: str, sub_agent_id: int) -> bool:
        """Whether *user_id* may run *sub_agent_id* right now — checked at every dispatch,
        because access can be revoked after a definition was shared.

        An inline ``automated`` agent travels with the definition: it is reachable for
        anyone holding a subscription to a definition that names it, so this one check
        covers both kinds. One EXISTS query (``ScheduledJobRepository.subscriber_can_run_agent``)
        rather than the accessible-agents listing, which hydrates every agent's config.
        """
        return await self.repo.subscriber_can_run_agent(db, user_id, sub_agent_id)

    async def _assert_groups_can_reach_agent(self, db: AsyncSession, sub_agent_id: int | None, group_ids: list[int]) -> None:
        """A definition may not be shared to a group whose members cannot reach its agent
        (and sharing never grants agent access). An inline automated agent is exempt:
        it is part of the definition and travels with it."""
        if sub_agent_id is None or not group_ids:
            return
        agent = await self.sub_agents.get_sub_agent_by_id(db, sub_agent_id)
        if agent is None:
            raise ValueError(f"Sub-agent {sub_agent_id} not found")
        if agent.type == SubAgentType.AUTOMATED:
            return
        for group_id in group_ids:
            try:
                await self.sub_agents.validate_agents_for_group(db, agent_ids=[sub_agent_id], group_id=group_id)
            except ValueError as e:
                raise ValueError(
                    f"Cannot share this job to group {group_id}: its members cannot reach the job's "
                    f"sub-agent '{agent.name}'. Share the agent with the group first. ({e})"
                ) from e

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    async def _permission(self, db: AsyncSession, definition_id: int, actor: User, is_admin: bool) -> str | None:
        """'owner' | 'write' | 'read' | None — an admin in admin mode reads as 'owner'."""
        perm = await self.repo.user_permission(db, definition_id, actor.id)
        if perm is None and is_admin:
            return "owner" if await self.repo.get_definition(db, definition_id) else None
        if is_admin and perm is not None:
            return "owner"
        return perm

    @staticmethod
    def _can_write(perm: str | None) -> bool:
        return perm in ("owner", "write")

    async def _require(self, db: AsyncSession, definition_id: int, actor: User, level: str, is_admin: bool) -> str:
        perm = await self._permission(db, definition_id, actor, is_admin)
        if perm is None:
            raise LookupError("Job not found")
        if level == "write" and not self._can_write(perm):
            raise SchedulerAccessError("You need write permission on this job to do that")
        if level == "owner" and perm != "owner":
            raise SchedulerAccessError("Only the owner (or an administrator) can do that")
        return perm

    # ------------------------------------------------------------------
    # Notifications (best effort, after commit)
    # ------------------------------------------------------------------

    async def _notify(self, db: AsyncSession, notifications: list[NotificationData]) -> None:
        if not notifications or self._notification_service is None:
            return
        try:
            await self._notification_service.bulk_create_notifications(db, notifications)
            await db.commit()
        except Exception:
            logger.exception("Failed to write %d scheduled-job notifications", len(notifications))

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    @staticmethod
    def _first_occurrence(definition: dict[str, Any], tz: str, now: datetime) -> tuple[datetime, bool, str | None]:
        """``(next_run_at, enabled, paused_reason)`` for a fresh subscription.

        A one-shot whose time has already passed arrives DISABLED with a reason: writing
        its past ``run_at`` as ``next_run_at`` with ``enabled`` would make the claim fire it
        on the next tick, once per new subscriber — the same state ``resume_job`` refuses
        to re-enable.
        """
        kind = ScheduleKind(definition["schedule_kind"])
        next_run_at = first_run_at(
            kind, definition.get("cron_expr"), definition.get("interval_seconds"), definition.get("run_at"), tz=tz, after=now
        )
        if kind == ScheduleKind.ONCE and next_run_at <= now:
            return next_run_at, False, "This one-time job already ran before you subscribed"
        return next_run_at, True, None

    async def _own_subscription_fields(
        self,
        db: AsyncSession,
        definition: dict[str, Any],
        user_id: str,
        delivery_channel_id: int | None,
        activated_by: str = "user",
        activated_by_groups: list[int] | None = None,
        subscriber_tz: str | None = None,
    ) -> dict[str, Any]:
        """The row for a fresh, inherited subscription of *user_id*.

        The first occurrence is computed in the SUBSCRIBER's timezone when the definition
        names none — the whole point of a null default timezone. *subscriber_tz* saves
        the lookup when the caller already resolved it.
        """
        tz = self._effective_tz(definition.get("timezone"), subscriber_tz) or await self._user_timezone(db, user_id)
        now = datetime.now(timezone.utc)
        next_run_at, enabled, paused_reason = self._first_occurrence(definition, tz, now)
        return {
            "definition_id": definition["id"],
            "user_id": user_id,
            "activated_by": activated_by,
            "activated_by_groups": json.dumps(activated_by_groups) if activated_by_groups else None,
            "next_run_at": next_run_at,
            "enabled": enabled,
            "paused_reason": paused_reason,
            "delivery_channel_id": delivery_channel_id,
            "created_at": now,
            "updated_at": now,
        }

    async def create_job(
        self,
        db: AsyncSession,
        data: ScheduledJobCreate,
        actor: User,
    ) -> ScheduledJob:
        """Create a definition owned by the caller and their own subscription to it."""

        # Watch jobs can run an agent too — when their condition is met — so an inline
        # sub-agent is created for either job type. What differs between them is the
        # trigger, not what runs.
        if not data.sub_agent_id and data.sub_agent_parameters is not None:
            # Create a new sub-agent based on the provided parameters and use its ID for the job
            sub_agent_parameters = SubAgentCreate(
                **data.sub_agent_parameters.model_dump(exclude_none=True),
                type=SubAgentType.AUTOMATED,
            )
            sub_agent = await self.sub_agents.create_sub_agent(db=db, actor=actor, data=sub_agent_parameters)
            data.sub_agent_id = sub_agent.id

        # SECURITY: Validate that user has access to the referenced sub-agent
        # This prevents users from creating jobs with sub-agents they can't access,
        # which would fail at execution time with confusing 403 errors
        if data.sub_agent_id is not None:
            accessible_agents = await self.schedulable_sub_agents(db, actor.id)
            if not any(sa.id == data.sub_agent_id for sa in accessible_agents):
                raise ValueError(
                    f"Access denied: You do not have permission to create jobs with sub-agent {data.sub_agent_id}"
                )

        if data.delivery_channel_id is not None:
            await self._validate_delivery_channel(db, data.delivery_channel_id)

        now = datetime.now(timezone.utc)
        # The definition keeps the timezone AS GIVEN: unset means each subscriber's own.
        # The creator's own subscription is evaluated in the creator's zone either way —
        # a naive run_at needs one to become an instant, and they typed the wall-clock.
        eval_tz = await self._resolve_timezone(db, data.timezone, actor.id)
        run_at = self._normalize_run_at(data.run_at, eval_tz)
        # Fail early on an unresolvable schedule, as before the split.
        first_run_at(data.schedule_kind, data.cron_expr, data.interval_seconds, run_at, tz=eval_tz, after=now)

        policy = data.trigger_policy or (
            TriggerPolicy.FIXED if data.job_type == JobType.WATCH else TriggerPolicy.OVERRIDABLE
        )
        definition_fields: dict[str, Any] = {
            "owner_user_id": actor.id,
            "sub_agent_id": data.sub_agent_id,
            "name": data.name,
            "job_type": data.job_type.value,
            "schedule_kind": data.schedule_kind.value,
            "cron_expr": data.cron_expr,
            "timezone": data.timezone or None,
            "interval_seconds": data.interval_seconds,
            "run_at": run_at,
            "trigger_policy": policy.value,
            "prompt": data.prompt,
            "notification_message": data.notification_message,
            "check_tool": data.check_tool,
            "check_args": json.dumps(data.check_args) if data.check_args is not None else None,
            "check_args_exprs": json.dumps(data.check_args_exprs) if data.check_args_exprs is not None else None,
            "cel_expr": data.cel_expr,
            "llm_condition": data.llm_condition,
            "destroy_after_trigger": data.destroy_after_trigger,
            "voice_call": data.voice_call,
            "max_failures": data.max_failures,
            "created_at": now,
            "updated_at": now,
        }
        definition_id = await self.repo.create_definition(db=db, actor=actor, fields=definition_fields)
        definition = {**definition_fields, "id": definition_id}
        sub_fields = await self._own_subscription_fields(
            db, definition, actor.id, data.delivery_channel_id, subscriber_tz=eval_tz
        )
        subscription_id = await self.repo.create_subscription(db=db, actor=actor, fields=sub_fields)
        await db.commit()
        result = await self.repo.get_job(db, subscription_id)
        assert result is not None
        return result

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def list_jobs(self, db: AsyncSession, user_id: str) -> list[ScheduledJob]:
        return await self.repo.list_jobs(db, user_id)

    async def get_job(self, db: AsyncSession, job_id: int, user_id: str) -> ScheduledJob | None:
        """The caller's own subscription *job_id*; another user's is a None (→ 404)."""
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != user_id:
            return None
        return job

    async def list_available_definitions(self, db: AsyncSession, user_id: str) -> list[SharedJobDefinition]:
        """Definitions the user may subscribe to or copy, with their subscription id if any."""
        return await self.repo.list_available_definitions(db, user_id)

    # ------------------------------------------------------------------
    # Update: one path, server-side routing
    # ------------------------------------------------------------------

    async def _recompute_inherited_next_runs(self, db: AsyncSession, actor: User, definition: dict[str, Any]) -> None:
        """After the defaults changed, every INHERITED subscription's next occurrence
        follows them — computed per subscriber, in their own timezone when the
        definition names none."""
        subs = await self.repo.list_subscriptions(db, definition["id"])
        inherited = [s for s in subs if s.trigger_inherited]
        tzs = await self.repo.user_timezones(db, [s.user_id for s in inherited])
        now = datetime.now(timezone.utc)
        for s in inherited:
            tz = self._effective_tz(definition.get("timezone"), tzs.get(s.user_id)) or default_timezone_name()
            next_run_at = first_run_at(
                ScheduleKind(definition["schedule_kind"]),
                definition.get("cron_expr"),
                definition.get("interval_seconds"),
                definition.get("run_at"),
                tz=tz,
                after=now,
            )
            await self.repo.update_subscription(
                db=db, actor=actor, subscription_id=s.id, fields={"next_run_at": next_run_at, "updated_at": now}
            )

    def _merge_trigger(
        self,
        data: ScheduledJobUpdate,
        current_kind: ScheduleKind,
        current_cron: str | None,
        current_interval: int | None,
        current_run_at: datetime | None,
        current_tz: str | None,
        eval_tz: str | None,
    ) -> dict[str, Any]:
        """The effective trigger after a partial PATCH, validated as a whole.

        The DB enforces exactly one schedule config: the column matching schedule_kind
        must be set and the other two NULL. A partial PATCH that switches kind must
        therefore clear the now-stale columns, and the effective combination must be
        validated here so callers get a clean 400 instead of a CheckViolationError.
        """
        new_kind = ScheduleKind(data.schedule_kind or current_kind)
        new_cron = data.cron_expr if data.cron_expr is not None else current_cron
        new_interval = data.interval_seconds if data.interval_seconds is not None else current_interval
        new_tz = data.timezone if data.timezone is not None else current_tz
        new_run_at = self._normalize_run_at(
            data.run_at if data.run_at is not None else current_run_at, new_tz or eval_tz
        )

        if new_kind == ScheduleKind.CRON:
            if not new_cron:
                raise ValueError("schedule_kind 'cron' requires cron_expr")
            new_interval = None
            new_run_at = None
        elif new_kind == ScheduleKind.INTERVAL:
            if new_interval is None:
                raise ValueError("schedule_kind 'interval' requires interval_seconds")
            new_cron = None
            new_run_at = None
        else:  # ScheduleKind.ONCE
            if new_run_at is None:
                raise ValueError("schedule_kind 'once' requires run_at")
            new_cron = None
            new_interval = None

        return {
            "schedule_kind": new_kind.value,
            "cron_expr": new_cron,
            "timezone": new_tz,
            "interval_seconds": new_interval,
            "run_at": new_run_at,
        }

    async def update_job(
        self,
        db: AsyncSession,
        job_id: int,
        data: ScheduledJobUpdate,
        actor: User,
        is_admin: bool = False,
        name: str | None = _UNSET,
        prompt: str | None = _UNSET,
        notification_message: str | None = _UNSET,
        check_tool: str | None = _UNSET,
        check_args_exprs: dict | None = _UNSET,
        cel_expr: str | None = _UNSET,
        llm_condition: str | None = _UNSET,
        destroy_after_trigger: bool | None = _UNSET,
        check_args: dict | None = _UNSET,
        delivery_channel_id: int | None = _UNSET,
        sub_agent_id: int | None = _UNSET,
        **kwargs,
    ) -> ScheduledJob | None:
        """Route each supplied field to the side of the split it lives on.

        Definition fields need ``write`` (owner, a group with write, or an admin);
        subscription fields are always the caller's own. The trigger is the one
        ambiguous group: with other subscribers it goes where ``data.scope`` says
        (default ``mine``); alone, a writer's edit lands on the defaults so a later
        share inherits it, and a plain reader's on their own override.
        """
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != actor.id:
            return None
        # What the routing below needs of the definition is already on the view.
        defaults = job.trigger_defaults
        assert defaults is not None
        definition: dict[str, Any] = {
            "id": job.definition_id,
            "name": job.name,
            "schedule_kind": defaults.schedule_kind.value,
            "cron_expr": defaults.cron_expr,
            "interval_seconds": defaults.interval_seconds,
            "run_at": defaults.run_at,
            "timezone": defaults.timezone,
        }
        perm = "owner" if is_admin else job.effective_permission
        now = datetime.now(timezone.utc)

        def_fields: dict[str, Any] = {}
        sub_fields: dict[str, Any] = {}

        # --- definition fields (the _UNSET pattern allows explicit None to clear) ---
        if name is not _UNSET:
            def_fields["name"] = name
        if prompt is not _UNSET:
            def_fields["prompt"] = prompt
        if notification_message is not _UNSET:
            def_fields["notification_message"] = notification_message
        if check_tool is not _UNSET:
            def_fields["check_tool"] = check_tool
        if check_args_exprs is not _UNSET:
            def_fields["check_args_exprs"] = json.dumps(check_args_exprs) if check_args_exprs is not None else None
        if cel_expr is not _UNSET:
            def_fields["cel_expr"] = cel_expr
        if llm_condition is not _UNSET:
            def_fields["llm_condition"] = llm_condition
        if destroy_after_trigger is not _UNSET:
            def_fields["destroy_after_trigger"] = destroy_after_trigger
        if check_args is not _UNSET:
            def_fields["check_args"] = json.dumps(check_args) if check_args is not None else None
        if sub_agent_id is not _UNSET:
            if sub_agent_id is None and job.job_type == JobType.TASK:
                raise ValueError("sub_agent_id cannot be cleared on a task job")
            def_fields["sub_agent_id"] = sub_agent_id
        for attr in ("max_failures", "voice_call"):
            val = getattr(data, attr, None)
            if val is not None:
                def_fields[attr] = val
        if data.trigger_policy is not None:
            def_fields["trigger_policy"] = data.trigger_policy.value

        # A value equal to what the definition already holds is not an edit. The console
        # resends the whole form, so without this a plain subscriber could never save
        # their own delivery or enabled state (every save would carry `name`), and an
        # owner's every save would bump the revision.
        for key in list(def_fields):
            if key in _DEFINITION_FIELDS and self._same_definition_value(job, key, def_fields[key]):
                del def_fields[key]

        # --- subscription fields ---
        if delivery_channel_id is not _UNSET:
            if delivery_channel_id is not None:
                await self._validate_delivery_channel(db, delivery_channel_id)
            sub_fields["delivery_channel_id"] = delivery_channel_id
        if data.enabled is not None:
            sub_fields["enabled"] = data.enabled
            # Toggling `enabled` is a deliberate stop or start, and the scheduler tells
            # those apart from one-shot retirement by `paused_reason`: the retry branch
            # of claim_due_jobs ignores `enabled` and trusts the reason instead. Either
            # direction also drops a pending retry — the user has made a decision about
            # the job, and a fresh attempt for an earlier interruption is not it.
            sub_fields["paused_reason"] = "Disabled by user" if data.enabled is False else None
            sub_fields["retry_at"] = None

        # --- trigger ---
        # Touched means CHANGED: the console resends every trigger field prefilled from
        # the job, and treating an unchanged echo as an edit would turn every save into a
        # private override (or, alone, a revision bump).
        trigger_touched = any(
            getattr(data, f) is not None and not self._same_trigger_value(job, f, getattr(data, f))
            for f in _TRIGGER_FIELDS
        )
        trigger_target: str | None = None
        if trigger_touched:
            others = job.subscriber_count > 1
            scope = data.scope
            if scope is None:
                # Alone, a writer's edit is the job's schedule; with others, the narrowest
                # effect. A reader alone (the owner unsubscribed) can only ever override.
                scope = "everyone" if (not others and self._can_write(perm)) else "mine"
            if scope == "everyone" and not self._can_write(perm):
                raise SchedulerAccessError("Changing everyone's schedule needs write permission on this job")
            # FIXED is enforced whatever the subscriber count: a sole read-only subscriber
            # must not be able to hammer the owner's check tool on their own tick either.
            if scope == "mine" and job.trigger_policy == TriggerPolicy.FIXED:
                raise ValueError(
                    "This job's schedule is fixed by its owner: subscribers cannot change their own. "
                    "Change the default with scope='everyone' (needs write) or ask the owner."
                )
            trigger_target = scope

            if scope == "everyone":
                merged = self._merge_trigger(
                    data,
                    ScheduleKind(definition["schedule_kind"]),
                    definition["cron_expr"],
                    definition["interval_seconds"],
                    definition["run_at"],
                    definition["timezone"],
                    eval_tz=job.timezone,
                )
                # Validate the schedule resolves before writing it.
                first_run_at(
                    ScheduleKind(merged["schedule_kind"]),
                    merged["cron_expr"],
                    merged["interval_seconds"],
                    merged["run_at"],
                    tz=merged["timezone"] or job.timezone,
                    after=now,
                )
                def_fields.update(merged)
                # "Everyone" includes the editor: their own override, if any, goes, so the
                # new default is what they see and what runs for them.
                if not job.trigger_inherited or job.timezone_override:
                    sub_fields.update(
                        {"schedule_kind": None, "cron_expr": None, "interval_seconds": None, "run_at": None, "timezone": None}
                    )
                    sub_fields["next_run_at"] = first_run_at(
                        ScheduleKind(merged["schedule_kind"]),
                        merged["cron_expr"],
                        merged["interval_seconds"],
                        merged["run_at"],
                        tz=merged["timezone"] or (await self._user_timezone(db, actor.id)),
                        after=now,
                    )
            else:
                merged = self._merge_trigger(
                    data,
                    job.schedule_kind,
                    job.cron_expr,
                    job.interval_seconds,
                    job.run_at,
                    # The override's own zone, if it names one; otherwise the override
                    # keeps following the inheritance chain until the user names one.
                    job.timezone_override,
                    eval_tz=job.timezone,
                )
                sub_fields.update(merged)
                sub_fields["next_run_at"] = first_run_at(
                    ScheduleKind(merged["schedule_kind"]),
                    merged["cron_expr"],
                    merged["interval_seconds"],
                    merged["run_at"],
                    tz=merged["timezone"] or job.timezone,
                    after=now,
                )

        if def_fields and not self._can_write(perm):
            raise SchedulerAccessError(
                "You can only change your own schedule, delivery and enabled state on a shared job; "
                "editing what it does needs write permission. Copy it to make your own version."
            )

        # A watch must keep at least one condition. Create rejects a watch with neither,
        # and WatchEvaluator treats that combination as unreachable — but a PATCH clearing
        # both produces exactly it, leaving a job that calls its check tool on every poll
        # and then fails until it auto-pauses. The effective pair is what matters, since
        # either half may be untouched by this request.
        if job.job_type == JobType.WATCH and ("cel_expr" in def_fields or "llm_condition" in def_fields):
            new_cel = def_fields["cel_expr"] if "cel_expr" in def_fields else job.cel_expr
            new_llm = def_fields["llm_condition"] if "llm_condition" in def_fields else job.llm_condition
            if not (new_cel or "").strip() and not (new_llm or "").strip():
                raise ValueError(
                    "A watch needs at least one of cel_expr or llm_condition; clearing "
                    "both would leave it with no condition to evaluate"
                )

        # SECURITY: a re-pointed agent must be reachable by the editor AND, on a shared
        # definition, by every group it is shared to — sharing must never be a side door.
        if def_fields.get("sub_agent_id") is not None:
            await self._assert_agent_accessible(db, actor.id, def_fields["sub_agent_id"], "use")
            groups = [p["user_group_id"] for p in await self.repo.get_permissions(db, job.definition_id)]
            await self._assert_groups_can_reach_agent(db, def_fields["sub_agent_id"], groups)

        reset_subscribers: list[int] = []
        if def_fields:
            def_fields["updated_at"] = now
            await self.repo.update_definition(db=db, actor=actor, definition_id=job.definition_id, fields=def_fields)
            definition = {**definition, **def_fields}
            if trigger_target == "everyone":
                await self._recompute_inherited_next_runs(db, actor, definition)
            # A policy flip to fixed is a reset: every override goes.
            if def_fields.get("trigger_policy") == TriggerPolicy.FIXED.value:
                reset_subscribers = await self._reset_overrides(db, actor, definition)
        if sub_fields:
            sub_fields["updated_at"] = now
            await self.repo.update_subscription(db=db, actor=actor, subscription_id=job_id, fields=sub_fields)
        await db.commit()

        if reset_subscribers:
            await self._notify_reset(db, actor, definition, reset_subscribers)
        return await self.repo.get_job(db, job_id)

    # ------------------------------------------------------------------
    # Delete / unsubscribe
    # ------------------------------------------------------------------

    async def delete_job(self, db: AsyncSession, job_id: int, actor: User, is_admin: bool = False) -> bool:
        """DELETE on my job: the definition (and every subscription) when I own it or am
        an admin; otherwise just my subscription — a subscriber cannot make the owner's
        job disappear. Returns False when *job_id* is not the caller's."""
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != actor.id:
            return False
        if is_admin or job.effective_permission == "owner":
            subscribers = await self.repo.delete_definition(db, actor, job.definition_id)
            await db.commit()
            await self._notify(
                db,
                [
                    NotificationData(
                        user_id=uid,
                        notification_type=NotificationType.JOB_DELETED,
                        title=f"Scheduled job deleted: {job.name}",
                        message=f"'{job.name}' was deleted by its owner, so it no longer runs for you.",
                        metadata={"definition_id": job.definition_id},
                    )
                    for uid in subscribers
                    if uid != actor.id
                ],
            )
        else:
            await self.repo.delete_subscription(db, actor, job_id)
            await db.commit()
        return True

    async def delete_definition(self, db: AsyncSession, definition_id: int, actor: User, is_admin: bool = False) -> None:
        """Delete a definition (and every subscription) by its own id — the owner's path
        when they have unsubscribed, and the admin's. Raises LookupError / SchedulerAccessError."""
        await self._require(db, definition_id, actor, "owner", is_admin)
        definition = await self.repo.get_definition(db, definition_id)
        assert definition is not None
        subscribers = await self.repo.delete_definition(db, actor, definition_id)
        await db.commit()
        await self._notify(
            db,
            [
                NotificationData(
                    user_id=uid,
                    notification_type=NotificationType.JOB_DELETED,
                    title=f"Scheduled job deleted: {definition['name']}",
                    message=f"'{definition['name']}' was deleted by its owner, so it no longer runs for you.",
                    metadata={"definition_id": definition_id},
                )
                for uid in subscribers
                if uid != actor.id
            ],
        )

    async def subscribe(self, db: AsyncSession, definition_id: int, actor: User, is_admin: bool = False) -> ScheduledJob:
        """Activate the caller on a definition they can read. Idempotent: an existing
        live subscription is returned as is."""
        await self._require(db, definition_id, actor, "read", is_admin)
        existing = await self.repo.get_subscription_for(db, definition_id, actor.id)
        if existing is not None:
            return existing
        definition = await self.repo.get_definition(db, definition_id)
        assert definition is not None
        # A regular agent must already be reachable; an inline automated one becomes
        # reachable BY subscribing (it travels with the definition), so it is not checked
        # here — the dispatcher re-checks either kind on every run.
        agent_id = definition.get("sub_agent_id")
        if (
            agent_id is not None
            and not await self.repo.agent_is_automated(db, agent_id)
            and not await self.subscriber_can_run_agent(db, actor.id, agent_id)
        ):
            raise ValueError("You cannot subscribe: this job runs a sub-agent you do not have access to")
        channel = await self._default_channel_for(db, definition)
        fields = await self._own_subscription_fields(db, definition, actor.id, channel, activated_by="user")
        subscription_id = await self.repo.create_subscription(db, actor, fields)
        await db.commit()
        job = await self.repo.get_job(db, subscription_id)
        assert job is not None
        return job

    async def unsubscribe(self, db: AsyncSession, definition_id: int, actor: User) -> bool:
        """Drop the caller's own subscription. The owner unsubscribing keeps the definition."""
        existing = await self.repo.get_subscription_for(db, definition_id, actor.id)
        if existing is None:
            return False
        await self.repo.delete_subscription(db, actor, existing.id)
        await db.commit()
        return True

    async def _default_channel_for(self, db: AsyncSession, definition: dict[str, Any]) -> int | None:
        """The channel a new subscription inherits: the owner's, since the channel is
        tenant-scoped and valid for every member of that tenant. The recipient is always
        the subscriber's own DM."""
        return await self.repo.owner_channel_id(db, definition["id"], definition["owner_user_id"])

    async def copy_definition(self, db: AsyncSession, definition_id: int, actor: User, is_admin: bool = False) -> ScheduledJob:
        """A new definition owned by the caller, initialised from one they can read, with
        their own subscription and no link back. Trigger defaults copied as values; an
        inline automated agent is copied with it; a referenced agent is referenced."""
        await self._require(db, definition_id, actor, "read", is_admin)
        src = await self.repo.get_definition(db, definition_id)
        assert src is not None
        agent_id = src.get("sub_agent_id")
        if agent_id is not None:
            agent = await self.sub_agents.get_sub_agent_by_id(db, agent_id)
            if agent is None:
                raise ValueError("The job's sub-agent no longer exists")
            if agent.type == SubAgentType.AUTOMATED:
                cfg = agent.config_version
                copy = await self.sub_agents.create_sub_agent(
                    db=db,
                    actor=actor,
                    data=SubAgentCreate(
                        name=agent.name,
                        description=(cfg.description if cfg else None) or f"Copy of {agent.name}",
                        type=SubAgentType.AUTOMATED,
                        model=cfg.model if cfg else None,
                        model_tier=cfg.model_tier if cfg else None,
                        system_prompt=cfg.system_prompt if cfg else None,
                        mcp_tools=cfg.mcp_tools if cfg else None,
                        enable_thinking=cfg.enable_thinking if cfg else None,
                        thinking_level=cfg.thinking_level if cfg else None,
                    ),
                )
                agent_id = copy.id
            elif not await self.subscriber_can_run_agent(db, actor.id, agent_id):
                raise ValueError(
                    f"Cannot copy: this job runs the sub-agent '{agent.name}', which you cannot access. "
                    "Ask for access to the agent first."
                )
        now = datetime.now(timezone.utc)
        fields = {
            k: src[k]
            for k in (
                "name",
                "job_type",
                "prompt",
                "check_tool",
                "cel_expr",
                "llm_condition",
                "destroy_after_trigger",
                "notification_message",
                "voice_call",
                "max_failures",
                "schedule_kind",
                "cron_expr",
                "interval_seconds",
                "run_at",
                "timezone",
                "trigger_policy",
            )
        }
        fields.update(
            {
                "owner_user_id": actor.id,
                "sub_agent_id": agent_id,
                "check_args": json.dumps(src["check_args"]) if src.get("check_args") is not None else None,
                "check_args_exprs": (
                    json.dumps(src["check_args_exprs"]) if src.get("check_args_exprs") is not None else None
                ),
                "created_at": now,
                "updated_at": now,
            }
        )
        new_id = await self.repo.create_definition(db, actor, fields)
        definition = {**fields, "id": new_id}
        mine = await self.repo.get_subscription_for(db, definition_id, actor.id)
        channel = mine.delivery_channel_id if mine else await self._default_channel_for(db, src)
        sub_fields = await self._own_subscription_fields(db, definition, actor.id, channel)
        subscription_id = await self.repo.create_subscription(db, actor, sub_fields)
        await db.commit()
        job = await self.repo.get_job(db, subscription_id)
        assert job is not None
        return job

    # ------------------------------------------------------------------
    # Subscription-level: pause / resume (mine)
    # ------------------------------------------------------------------

    async def pause_job(self, db: AsyncSession, job_id: int, actor: User, reason: str = "Manually paused") -> bool:
        """Disable the caller's own subscription."""
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != actor.id:
            return False
        await self.repo.update_subscription(
            db=db,
            actor=actor,
            subscription_id=job_id,
            # A pending retry dies with the pause: a job somebody stopped is not resumed
            # by a fresh attempt for an interruption that happened before they did.
            fields={
                "enabled": False,
                "paused_reason": reason,
                "retry_at": None,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        await db.commit()
        return True

    async def resume_job(self, db: AsyncSession, job_id: int, actor: User) -> bool:
        """Re-enable the caller's own subscription and reset its failure counter."""
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != actor.id:
            return False
        # A once-job keeps its past run_at as next_run_at after completing, so
        # re-enabling it would make the engine claim and re-execute it on the
        # next tick — refuse instead of silently re-running a finished job.
        if job.schedule_kind == ScheduleKind.ONCE:
            ref = job.run_at or job.next_run_at
            if ref is None or ref <= datetime.now(timezone.utc):
                raise ValueError(
                    "This one-time job has already run; create a new job instead of resuming it."
                )
        next_run_at = compute_next_run(
            job.schedule_kind, job.cron_expr, job.interval_seconds, job.run_at, tz=job.timezone
        )
        fields: dict = {
            "enabled": True,
            "consecutive_failures": 0,
            "paused_reason": None,
            # A retry earned while the job was paused must not fire the moment it is
            # resumed: the resume computes the next occurrence, and that is the run
            # the user asked for.
            "retry_at": None,
            "updated_at": datetime.now(timezone.utc),
        }
        if next_run_at:
            fields["next_run_at"] = next_run_at
        await self.repo.update_subscription(db=db, actor=actor, subscription_id=job_id, fields=fields)
        await db.commit()
        return True

    # ------------------------------------------------------------------
    # Definition-level: suspend / unsuspend / reset overrides / public
    # ------------------------------------------------------------------

    async def _subscriber_ids(self, db: AsyncSession, definition_id: int, except_user: str) -> list[str]:
        return [s.user_id for s in await self.repo.list_subscriptions(db, definition_id) if s.user_id != except_user]

    async def suspend(
        self, db: AsyncSession, definition_id: int, actor: User, reason: str | None, is_admin: bool = False
    ) -> None:
        """Stop every subscription of a definition, preserving each member's ``enabled``."""
        await self._require(db, definition_id, actor, "write", is_admin)
        definition = await self.repo.get_definition(db, definition_id)
        assert definition is not None
        await self.repo.set_suspended(db, actor, definition_id, True, reason)
        await db.commit()
        await self._notify(
            db,
            [
                NotificationData(
                    user_id=uid,
                    notification_type=NotificationType.JOB_SUSPENDED,
                    title=f"Scheduled job suspended: {definition['name']}",
                    message=(
                        f"'{definition['name']}' was suspended for everyone"
                        + (f": {reason}" if reason else ".")
                        + " It will not run until it is resumed; your own settings are kept."
                    ),
                    metadata={"definition_id": definition_id, "suspended_by": actor.id},
                )
                for uid in await self._subscriber_ids(db, definition_id, actor.id)
            ],
        )

    async def unsuspend(self, db: AsyncSession, definition_id: int, actor: User, is_admin: bool = False) -> None:
        """Lift a suspension. Each subscription resumes where its own ``enabled`` left it,
        with its next occurrence recomputed so nothing fires for the time it was suspended."""
        await self._require(db, definition_id, actor, "write", is_admin)
        definition = await self.repo.get_definition(db, definition_id)
        assert definition is not None
        await self.repo.set_suspended(db, actor, definition_id, False)
        now = datetime.now(timezone.utc)
        for s in await self.repo.list_subscriptions(db, definition_id):
            if s.enabled and s.schedule_kind != ScheduleKind.ONCE and s.next_run_at < now:
                nxt = compute_next_run(s.schedule_kind, s.cron_expr, s.interval_seconds, s.run_at, tz=s.timezone)
                if nxt:
                    await self.repo.update_subscription(db, actor, s.id, {"next_run_at": nxt, "updated_at": now})
        await db.commit()
        await self._notify(
            db,
            [
                NotificationData(
                    user_id=uid,
                    notification_type=NotificationType.JOB_RESUMED,
                    title=f"Scheduled job resumed: {definition['name']}",
                    message=f"'{definition['name']}' runs again for everyone who has it enabled.",
                    metadata={"definition_id": definition_id},
                )
                for uid in await self._subscriber_ids(db, definition_id, actor.id)
            ],
        )

    async def _reset_overrides(self, db: AsyncSession, actor: User, definition: dict[str, Any]) -> list[int]:
        """Clear every override; returns the ids of the subscriptions that had one."""
        subs = await self.repo.list_subscriptions(db, definition["id"])
        overridden = [s for s in subs if not s.trigger_inherited]
        tzs = await self.repo.user_timezones(db, [s.user_id for s in overridden])
        now = datetime.now(timezone.utc)
        next_runs: dict[int, datetime] = {}
        for s in overridden:
            tz = self._effective_tz(definition.get("timezone"), tzs.get(s.user_id)) or default_timezone_name()
            next_runs[s.id] = first_run_at(
                ScheduleKind(definition["schedule_kind"]),
                definition.get("cron_expr"),
                definition.get("interval_seconds"),
                definition.get("run_at"),
                tz=tz,
                after=now,
            )
        return await self.repo.clear_trigger_overrides(db, actor, definition["id"], next_runs)

    async def _notify_reset(
        self, db: AsyncSession, actor: User, definition: dict[str, Any], subscription_ids: list[int]
    ) -> None:
        subs = {s.id: s for s in await self.repo.list_subscriptions(db, definition["id"])}
        await self._notify(
            db,
            [
                NotificationData(
                    user_id=subs[sid].user_id,
                    notification_type=NotificationType.JOB_SUBSCRIPTION_RESET,
                    title=f"Schedule reset: {definition['name']}",
                    message=(
                        f"Your own schedule for '{definition['name']}' was reset to the job's default "
                        "by its owner."
                    ),
                    metadata={"definition_id": definition["id"], "job_id": sid},
                )
                for sid in subscription_ids
                if sid in subs and subs[sid].user_id != actor.id
            ],
        )

    async def reset_overrides(self, db: AsyncSession, definition_id: int, actor: User, is_admin: bool = False) -> int:
        """Writer action: every subscription follows the defaults again. Returns how many changed."""
        await self._require(db, definition_id, actor, "write", is_admin)
        definition = await self.repo.get_definition(db, definition_id)
        assert definition is not None
        reset = await self._reset_overrides(db, actor, definition)
        await db.commit()
        await self._notify_reset(db, actor, definition, reset)
        return len(reset)

    async def set_public(self, db: AsyncSession, definition_id: int, actor: User, is_public: bool) -> None:
        """Admin only (the router enforces it): a curated org-wide template."""
        if await self.repo.get_definition(db, definition_id) is None:
            raise LookupError("Job not found")
        await self.repo.update_definition(
            db, actor, definition_id, {"is_public": is_public, "updated_at": datetime.now(timezone.utc)}, bump_revision=False
        )
        await db.commit()

    # ------------------------------------------------------------------
    # Permissions (sharing)
    # ------------------------------------------------------------------

    async def get_permissions(
        self, db: AsyncSession, definition_id: int, actor: User, is_admin: bool = False
    ) -> list[dict[str, Any]]:
        await self._require(db, definition_id, actor, "write", is_admin)
        return await self.repo.get_permissions(db, definition_id)

    async def update_permissions(
        self,
        db: AsyncSession,
        definition_id: int,
        group_permissions: list[dict[str, Any]],
        actor: User,
        is_admin: bool = False,
    ) -> None:
        """Replace the group grants of a definition and tell the people it affects.

        Members of a group losing its grant lose their standing: a subscription that
        group's default created is removed, a self-made one is disabled with a reason —
        unless they still reach the definition another way.
        """
        await self._require(db, definition_id, actor, "write", is_admin)
        definition = await self.repo.get_definition(db, definition_id)
        assert definition is not None
        # Write implies read: a grant of ['write'] alone would list the job to a read-role
        # member and then refuse their subscribe.
        group_permissions = [
            {**p, "permissions": sorted(set(p["permissions"]) | ({"read"} if "write" in p["permissions"] else set()))}
            for p in group_permissions
        ]
        new_groups = [p["user_group_id"] for p in group_permissions]
        await self._assert_groups_can_reach_agent(db, definition.get("sub_agent_id"), new_groups)

        added, removed, changed = await self.repo.replace_permissions(db, actor, definition_id, group_permissions)
        for group_id in removed:
            await self.repo.remove_group_default(db, group_id, definition_id, actor)
            members = await self.repo.group_member_ids(db, [group_id])
            await self._withdraw_access(db, actor, [definition_id], members, group_id)
        await db.commit()

        name = definition["name"]
        notifications: list[NotificationData] = []
        for groups, ntype, title, message in (
            (
                added,
                NotificationType.JOB_SHARED,
                f"Scheduled job shared with you: {name}",
                f"The scheduled job '{name}' has been shared with your group. Subscribe to run it under your own account.",
            ),
            (
                removed,
                NotificationType.JOB_ACCESS_REVOKED,
                f"Access to scheduled job revoked: {name}",
                f"Your group's access to the scheduled job '{name}' has been revoked.",
            ),
            (
                changed,
                NotificationType.JOB_PERMISSION_CHANGED,
                f"Permissions changed for scheduled job: {name}",
                f"Your group's permissions on the scheduled job '{name}' have been updated.",
            ),
        ):
            if not groups:
                continue
            for uid in await self.repo.group_member_ids(db, list(groups)):
                if uid != actor.id:
                    notifications.append(
                        NotificationData(
                            user_id=uid,
                            notification_type=ntype,
                            title=title,
                            message=message,
                            metadata={"definition_id": definition_id},
                        )
                    )
        await self._notify(db, notifications)

    async def _withdraw_access(
        self, db: AsyncSession, actor: User, definition_ids: list[int], user_ids: list[str], group_id: int
    ) -> None:
        """Members of *group_id* no longer reach *definition_ids* through it. For each
        live subscription: a group-default one from THIS group loses the group and, if no
        other group stands behind it, is removed; a self-made one is disabled with
        "access revoked" when the member has no other way to the definition."""
        rows = await self.repo.group_backed_subscriptions(db, definition_ids, user_ids, group_id)
        now = datetime.now(timezone.utc)
        for row in rows:
            still_reaches = await self.repo.user_permission(
                db, row["definition_id"], row["user_id"], ignore_group=group_id
            ) is not None
            if row["via_group"]:
                await self.repo.remove_group_from_subscription(db, actor, row["id"], group_id)
                remaining = [g for g in (row["groups"] or []) if g != group_id]
                if row["activated_by"] == "group" and not remaining:
                    await self.repo.delete_subscription(db, actor, row["id"])
                    continue
            if not still_reaches:
                await self.repo.update_subscription(
                    db,
                    actor,
                    row["id"],
                    {"enabled": False, "paused_reason": _ACCESS_REVOKED_REASON, "retry_at": None, "updated_at": now},
                )

    # ------------------------------------------------------------------
    # Group defaults
    # ------------------------------------------------------------------

    async def list_group_definitions(self, db: AsyncSession, group_id: int) -> list[dict[str, Any]]:
        """Definitions shared to a group, flagged with which are its defaults."""
        return await self.repo.list_group_definitions(db, group_id)

    async def _activate_default(self, db: AsyncSession, actor: User, group_id: int, definition_id: int, user_ids: list[str]) -> None:
        """Subscribe *user_ids* to *definition_id* as a group default — enabled, inherited,
        each first occurrence in the member's own timezone — and DM-less consent notice
        in the console for everyone whose subscription was created."""
        definition = await self.repo.get_definition(db, definition_id)
        if definition is None or not user_ids:
            return
        channel = await self._default_channel_for(db, definition)
        tzs = await self.repo.user_timezones(db, user_ids)
        now = datetime.now(timezone.utc)
        activations = []
        for uid in user_ids:
            tz = self._effective_tz(definition.get("timezone"), tzs.get(uid)) or default_timezone_name()
            next_run_at, enabled, paused_reason = self._first_occurrence(definition, tz, now)
            activations.append(
                {
                    "user_id": uid,
                    "delivery_channel_id": channel,
                    "next_run_at": next_run_at,
                    "enabled": enabled,
                    "paused_reason": paused_reason,
                    "_was_revoked": True,
                }
            )
        created = await self.repo.bulk_subscribe(
            db, actor, definition_id, activations, "group", group_id, revoked_reason=_ACCESS_REVOKED_REASON
        )
        if created and self._notification_service is not None:
            await self._notification_service.bulk_create_notifications(
                db,
                [
                    NotificationData(
                        user_id=uid,
                        notification_type=NotificationType.JOB_SUBSCRIPTION_ACTIVATED,
                        title=f"Scheduled job activated for you: {definition['name']}",
                        message=(
                            f"'{definition['name']}' now runs under your account because it is a default job "
                            "of one of your groups. You can disable it or change its delivery at any time."
                        ),
                        metadata={"definition_id": definition_id, "group_id": group_id, "activated_by": actor.id},
                    )
                    for uid in created
                ],
            )

    async def set_group_default_jobs(
        self, db: AsyncSession, group_id: int, definition_ids: list[int], actor: User
    ) -> None:
        """Set (replace) a group's default jobs. Only definitions already shared to the
        group qualify — a default never grants access by itself. Does not commit."""
        for did in definition_ids:
            if not await self.repo.group_has_grant(db, did, group_id):
                raise ValueError(f"Job definition {did} is not shared with this group. Share it first.")
        current = set(await self.repo.get_group_default_definition_ids(db, group_id))
        wanted = set(definition_ids)
        members = await self.repo.group_member_ids(db, [group_id])
        for did in wanted - current:
            await self.repo.add_group_default(db, group_id, did, actor)
            await self._activate_default(db, actor, group_id, did, members)
        for did in current - wanted:
            await self.repo.remove_group_default(db, group_id, did, actor)
            await self._withdraw_default(db, actor, group_id, did, members)

    async def add_group_default_job(self, db: AsyncSession, group_id: int, definition_id: int, actor: User) -> None:
        if not await self.repo.group_has_grant(db, definition_id, group_id):
            raise ValueError("This job is not shared with the group. Share it first.")
        if await self.repo.add_group_default(db, group_id, definition_id, actor):
            await self._activate_default(db, actor, group_id, definition_id, await self.repo.group_member_ids(db, [group_id]))

    async def remove_group_default_job(self, db: AsyncSession, group_id: int, definition_id: int, actor: User) -> None:
        if await self.repo.remove_group_default(db, group_id, definition_id, actor):
            await self._withdraw_default(db, actor, group_id, definition_id, await self.repo.group_member_ids(db, [group_id]))

    async def _withdraw_default(
        self, db: AsyncSession, actor: User, group_id: int, definition_id: int, user_ids: list[str]
    ) -> None:
        """A definition stops being the group's default: subscriptions that only this
        default stood behind are removed; self-made ones stay (access is unchanged)."""
        for row in await self.repo.group_backed_subscriptions(db, [definition_id], user_ids, group_id):
            if not row["via_group"]:
                continue
            await self.repo.remove_group_from_subscription(db, actor, row["id"], group_id)
            remaining = [g for g in (row["groups"] or []) if g != group_id]
            if row["activated_by"] == "group" and not remaining:
                await self.repo.delete_subscription(db, actor, row["id"])

    async def on_members_added(self, db: AsyncSession, actor: User, group_id: int, user_ids: list[str]) -> None:
        """Mirror of default-agent activation on join: every default job of the group
        becomes a subscription of each new member. Does not commit."""
        for did in await self.repo.get_group_default_definition_ids(db, group_id):
            await self._activate_default(db, actor, group_id, did, user_ids)

    async def on_group_deleted(self, db: AsyncSession, actor: User, group_id: int) -> None:
        """A group is being (soft-)deleted: every grant through it is gone. Its default
        rows are removed (a soft delete fires no FK cascade) and each member's standing
        is withdrawn as on leave. Runs BEFORE the group row is soft-deleted, while the
        membership can still be read. Does not commit."""
        members = await self.repo.group_member_ids(db, [group_id])
        for did in await self.repo.group_default_ids_for_group(db, group_id):
            await self.repo.remove_group_default(db, group_id, did, actor)
        shared = await self.repo.definitions_shared_to_group(db, group_id)
        if shared and members:
            await self._withdraw_access(db, actor, shared, members, group_id)

    async def on_members_removed(self, db: AsyncSession, actor: User, group_id: int, user_ids: list[str]) -> None:
        """On leave: group-default subscriptions from this group are removed; self-made
        ones on a grant the member no longer holds are disabled with "access revoked",
        so re-adding restores their customisation. Does not commit."""
        shared = await self.repo.definitions_shared_to_group(db, group_id)
        if shared:
            await self._withdraw_access(db, actor, shared, user_ids, group_id)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    async def list_runs(
        self,
        db: AsyncSession,
        job_id: int,
        user_id: str,
        limit: int = 50,
    ) -> list[ScheduledJobRun] | None:
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != user_id:
            return None
        return await self.repo.list_runs(db, job_id, limit)

    async def get_run(
        self,
        db: AsyncSession,
        job_id: int,
        run_id: int,
        user_id: str,
    ) -> ScheduledJobRun | None:
        """Fetch one run of the user's job by id — unlike list_runs, not capped
        to the newest 50, so arbitrarily old runs stay resolvable (conversation
        adoption looks up the run a thread reply refers to)."""
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != user_id:
            return None
        return await self.repo.get_run(db, job_id, run_id)
