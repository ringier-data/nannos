"""Scheduler service — CRUD operations for scheduled jobs."""

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

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
)
from ..models.user import User
from ..repositories.scheduled_job_repository import ScheduledJobRepository, compute_next_run
from ..utils.timezones import default_timezone_name, resolve_timezone, validate_timezone_name

# Sentinel value to distinguish "no change" from "set to None"
_UNSET: Any = object()

if TYPE_CHECKING:
    from ..repositories.delivery_channel_repository import DeliveryChannelRepository

logger = logging.getLogger(__name__)


class SchedulerService:
    """CRUD service for scheduled jobs."""

    def __init__(
        self, repository: ScheduledJobRepository | None = None, sub_agent_service: SubAgentService | None = None
    ) -> None:
        self._repo = repository
        self._sub_agent_service = sub_agent_service
        self._delivery_channel_repo: "DeliveryChannelRepository | None" = None
        self._user_settings_service: UserSettingsService | None = None

    def set_repository(self, repository: ScheduledJobRepository) -> None:
        self._repo = repository

    def set_sub_agent_service(self, sub_agent_service: SubAgentService) -> None:
        self._sub_agent_service = sub_agent_service

    def set_user_settings_service(self, user_settings_service: UserSettingsService) -> None:
        self._user_settings_service = user_settings_service

    def set_delivery_channel_repository(self, repository: "DeliveryChannelRepository") -> None:
        self._delivery_channel_repo = repository

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

    async def _resolve_timezone(self, db: AsyncSession, requested: str | None, user_id: str) -> str:
        """Return the job timezone: explicit request wins, else the user's settings timezone."""
        if requested:
            return requested
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

    @staticmethod
    def _normalize_run_at(run_at: datetime | None, tz: str | None) -> datetime | None:
        """Attach the job timezone to a naive run_at.

        The frontend's datetime-local input submits wall-clock strings without an
        offset; storing them unmodified makes Postgres read them as UTC.
        """
        if run_at is not None and run_at.tzinfo is None:
            return run_at.replace(tzinfo=resolve_timezone(tz))
        return run_at

    async def create_job(
        self,
        db: AsyncSession,
        data: ScheduledJobCreate,
        actor: User,
    ) -> ScheduledJob:
        """Create a new scheduled job for the authenticated user."""

        if data.job_type == "task" and not data.sub_agent_id and data.sub_agent_parameters is not None:
            if self._sub_agent_service is None:
                raise RuntimeError("SubAgentService not injected. Call set_sub_agent_service() during initialization.")
            # Create a new sub-agent based on the provided parameters and use its ID for the job
            sub_agent_parameters = SubAgentCreate(
                **data.sub_agent_parameters.model_dump(exclude_none=True),
                type=SubAgentType.AUTOMATED,
            )
            sub_agent = await self._sub_agent_service.create_sub_agent(
                db=db,
                actor=actor,
                data=sub_agent_parameters,
            )
            data.sub_agent_id = sub_agent.id

        # SECURITY: Validate that user has access to the referenced sub-agent
        # This prevents users from creating jobs with sub-agents they can't access,
        # which would fail at execution time with confusing 403 errors
        if data.sub_agent_id is not None:
            if self._sub_agent_service is None:
                raise RuntimeError("SubAgentService not injected. Call set_sub_agent_service() during initialization.")
            accessible_agents = await self._sub_agent_service.get_accessible_sub_agents(db, actor.id)
            if not any(sa.id == data.sub_agent_id for sa in accessible_agents):
                raise ValueError(
                    f"Access denied: You do not have permission to create jobs with sub-agent {data.sub_agent_id}"
                )

        if data.delivery_channel_id is not None:
            await self._validate_delivery_channel(db, data.delivery_channel_id)

        now = datetime.now(timezone.utc)

        tz = await self._resolve_timezone(db, data.timezone, actor.id)
        run_at = self._normalize_run_at(data.run_at, tz)

        # Compute initial next_run_at
        next_run_at = compute_next_run(
            schedule_kind=data.schedule_kind,
            cron_expr=data.cron_expr,
            interval_seconds=data.interval_seconds,
            run_at=run_at,
            after=now,
            tz=tz,
        )
        if next_run_at is None:
            # once-only job — run_at is the first and only run
            next_run_at = run_at  # type: ignore[assignment]

        fields: dict = {
            "user_id": actor.id,
            "sub_agent_id": data.sub_agent_id,
            "name": data.name,
            "job_type": data.job_type.value,
            "schedule_kind": data.schedule_kind.value,
            "cron_expr": data.cron_expr,
            "timezone": tz,
            "interval_seconds": data.interval_seconds,
            "run_at": run_at,
            "next_run_at": next_run_at,
            "prompt": data.prompt,
            "notification_message": data.notification_message,
            "check_tool": data.check_tool,
            "check_args": json.dumps(data.check_args) if data.check_args is not None else None,
            "condition_expr": data.condition_expr,
            "expected_value": data.expected_value,
            "llm_condition": data.llm_condition,
            "destroy_after_trigger": data.destroy_after_trigger,
            "delivery_channel_id": data.delivery_channel_id,
            "voice_call": data.voice_call,
            "max_failures": data.max_failures,
            "created_at": now,
            "updated_at": now,
        }

        job_id = await self.repo.create_job(db=db, actor=actor, fields=fields)
        await db.commit()
        result = await self.repo.get_job(db, job_id)
        assert result is not None
        return result

    async def list_jobs(self, db: AsyncSession, user_id: str) -> list[ScheduledJob]:
        return await self.repo.list_jobs(db, user_id)

    async def get_job(self, db: AsyncSession, job_id: int, user_id: str) -> ScheduledJob | None:
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != user_id:
            return None
        return job

    async def update_job(
        self,
        db: AsyncSession,
        job_id: int,
        data: ScheduledJobUpdate,
        actor: User,
        name: str | None = _UNSET,
        prompt: str | None = _UNSET,
        notification_message: str | None = _UNSET,
        check_tool: str | None = _UNSET,
        condition_expr: str | None = _UNSET,
        expected_value: str | None = _UNSET,
        llm_condition: str | None = _UNSET,
        destroy_after_trigger: bool | None = _UNSET,
        check_args: dict | None = _UNSET,
        delivery_channel_id: int | None = _UNSET,
        sub_agent_id: int | None = _UNSET,
        **kwargs,
    ) -> ScheduledJob | None:
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != actor.id:
            return None

        fields: dict = {"updated_at": datetime.now(timezone.utc)}

        # Handle fields with _UNSET pattern (allows explicit None to clear)
        if name is not _UNSET:
            fields["name"] = name
        if prompt is not _UNSET:
            fields["prompt"] = prompt
        if notification_message is not _UNSET:
            fields["notification_message"] = notification_message
        if check_tool is not _UNSET:
            fields["check_tool"] = check_tool
        if condition_expr is not _UNSET:
            fields["condition_expr"] = condition_expr
        if expected_value is not _UNSET:
            fields["expected_value"] = expected_value
        if llm_condition is not _UNSET:
            fields["llm_condition"] = llm_condition
        if destroy_after_trigger is not _UNSET:
            fields["destroy_after_trigger"] = destroy_after_trigger
        if check_args is not _UNSET:
            fields["check_args"] = json.dumps(check_args) if check_args is not None else None
        if delivery_channel_id is not _UNSET:
            if delivery_channel_id is not None:
                await self._validate_delivery_channel(db, delivery_channel_id)
            fields["delivery_channel_id"] = delivery_channel_id
        if sub_agent_id is not _UNSET:
            if sub_agent_id is None and job.job_type == JobType.TASK:
                raise ValueError("sub_agent_id cannot be cleared on a task job")
            fields["sub_agent_id"] = sub_agent_id

        # Handle fields that still use old pattern (from kwargs/data)
        for attr in ("enabled", "max_failures", "voice_call"):
            val = getattr(data, attr, None)
            if val is not None:
                fields[attr] = val

        # SECURITY: Validate that user has access to the referenced sub-agent
        # if sub_agent_id is being updated
        if "sub_agent_id" in fields and fields["sub_agent_id"] is not None:
            if self._sub_agent_service is None:
                raise RuntimeError("SubAgentService not injected. Call set_sub_agent_service() during initialization.")
            accessible_agents = await self._sub_agent_service.get_accessible_sub_agents(db, actor.id)
            if not any(sa.id == fields["sub_agent_id"] for sa in accessible_agents):
                raise ValueError(f"Access denied: You do not have permission to use sub-agent {fields['sub_agent_id']}")

        # The DB enforces exactly one schedule config (scheduled_jobs_schedule_config):
        # the column matching schedule_kind must be set and the other two NULL. A
        # partial PATCH that switches kind must therefore clear the now-stale columns,
        # and the effective combination must be validated here so callers get a clean
        # 400 instead of a CheckViolationError surfacing as a 500.
        if any(
            getattr(data, f) is not None
            for f in ("schedule_kind", "cron_expr", "interval_seconds", "run_at", "timezone")
        ):
            new_kind = ScheduleKind(data.schedule_kind or job.schedule_kind)
            new_cron = data.cron_expr if data.cron_expr is not None else job.cron_expr
            new_interval = data.interval_seconds if data.interval_seconds is not None else job.interval_seconds
            new_tz = data.timezone if data.timezone is not None else job.timezone
            new_run_at = self._normalize_run_at(data.run_at if data.run_at is not None else job.run_at, new_tz)

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

            fields["schedule_kind"] = new_kind.value
            fields["cron_expr"] = new_cron
            fields["timezone"] = new_tz
            fields["interval_seconds"] = new_interval
            fields["run_at"] = new_run_at
            # compute_next_run returns None for 'once' (nothing to repeat after the
            # run) — the single run happens at run_at itself, mirroring create_job.
            fields["next_run_at"] = (
                compute_next_run(new_kind, new_cron, new_interval, new_run_at, tz=new_tz) or new_run_at
            )

        await self.repo.update_job(db=db, actor=actor, job_id=job_id, fields=fields)
        await db.commit()
        return await self.repo.get_job(db, job_id)

    async def delete_job(self, db: AsyncSession, job_id: int, actor: User) -> bool:
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != actor.id:
            return False
        await self.repo.delete_job(db=db, actor=actor, job_id=job_id)
        await db.commit()
        return True

    async def pause_job(self, db: AsyncSession, job_id: int, actor: User, reason: str = "Manually paused") -> bool:
        job = await self.repo.get_job(db, job_id)
        if job is None or job.user_id != actor.id:
            return False
        await self.repo.update_job(
            db=db,
            actor=actor,
            job_id=job_id,
            fields={"enabled": False, "paused_reason": reason, "updated_at": datetime.now(timezone.utc)},
        )
        await db.commit()
        return True

    async def resume_job(self, db: AsyncSession, job_id: int, actor: User) -> bool:
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
        # Reset failures and re-enable
        next_run_at = compute_next_run(
            job.schedule_kind, job.cron_expr, job.interval_seconds, job.run_at, tz=job.timezone
        )
        fields: dict = {
            "enabled": True,
            "consecutive_failures": 0,
            "paused_reason": None,
            "updated_at": datetime.now(timezone.utc),
        }
        if next_run_at:
            fields["next_run_at"] = next_run_at
        await self.repo.update_job(db=db, actor=actor, job_id=job_id, fields=fields)
        await db.commit()
        return True

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
