"""Unit tests for SchedulerService.

Tests the service-layer logic using mocked repository and sub-agent service,
so no database container is needed.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from console_backend.models.scheduled_job import (
    AutomatedSubAgentConfig,
    JobType,
    ScheduledJob,
    ScheduledJobCreate,
    ScheduledJobUpdate,
    ScheduleKind,
)
from console_backend.models.user import User, UserRole, UserSettings, UserStatus
from console_backend.services.scheduler_service import SchedulerService


def _make_user(user_id: str = "user-123") -> User:
    return User(
        id=user_id,
        sub="sub-" + user_id,
        email="test@example.com",
        first_name="Test",
        last_name="User",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )


def _make_job(
    job_id: int = 1,
    user_id: str = "user-123",
    sub_agent_id: int | None = 42,
    job_type: JobType = JobType.TASK,
    schedule_kind: ScheduleKind = ScheduleKind.INTERVAL,
    interval_seconds: int | None = 3600,
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
        enabled=True,
        max_failures=3,
        consecutive_failures=0,
        created_at=now,
        updated_at=now,
    )


def _make_interval_create(sub_agent_id: int | None = 42) -> ScheduledJobCreate:
    return ScheduledJobCreate(
        sub_agent_id=sub_agent_id,
        name="My Hourly Task",
        job_type=JobType.TASK,
        schedule_kind=ScheduleKind.INTERVAL,
        interval_seconds=3600,
        prompt="Do something useful",
    )


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_sub_agent_service():
    svc = AsyncMock()
    return svc


@pytest.fixture
def mock_delivery_channel_repo():
    repo = AsyncMock()
    # Default: any referenced channel exists.
    repo.get_channel_by_id.return_value = MagicMock()
    return repo


@pytest.fixture
def mock_user_settings_service():
    svc = AsyncMock()
    # Explicit timezone — the model default follows the DEFAULT_TIMEZONE env
    # var, which must not leak into these assertions.
    svc.get_settings.return_value = UserSettings(user_id="user-123", timezone="Europe/Zurich")
    return svc


@pytest.fixture
def service(
    mock_repo, mock_sub_agent_service, mock_delivery_channel_repo, mock_user_settings_service
) -> SchedulerService:
    s = SchedulerService()
    s.set_repository(mock_repo)
    s.set_sub_agent_service(mock_sub_agent_service)
    s.set_delivery_channel_repository(mock_delivery_channel_repo)
    s.set_user_settings_service(mock_user_settings_service)
    return s


@pytest.fixture
def actor() -> User:
    return _make_user()


class TestCreateJobAutoSubAgent:
    """When sub_agent_parameters is provided, a sub-agent is created automatically."""

    @pytest.mark.asyncio
    async def test_auto_creates_sub_agent_and_uses_its_id(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """create_job() calls sub_agent_service.create_sub_agent() and uses the new agent's ID."""
        db = AsyncMock()
        created_agent = MagicMock()
        created_agent.id = 99

        mock_sub_agent_service.create_sub_agent.return_value = created_agent
        # Accessible agents — newly created agent included
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [created_agent]

        mock_repo.create_job.return_value = 1
        expected_job = _make_job(job_id=1, sub_agent_id=99)
        mock_repo.get_job.return_value = expected_job

        create_data = ScheduledJobCreate(
            sub_agent_id=None,
            sub_agent_parameters=AutomatedSubAgentConfig(
                name="Auto Agent",
                description="Does daily reporting",
                model="claude-sonnet-4.5",
                system_prompt="You are a daily reporter. Generate a short summary.",
            ),
            name="Daily Report Job",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=86400,
            prompt="Generate daily report",
        )

        result = await service.create_job(db=db, data=create_data, actor=actor)

        # Sub-agent was created once
        mock_sub_agent_service.create_sub_agent.assert_awaited_once()

        # The new sub-agent's ID was passed to repo.create_job
        create_call_fields = mock_repo.create_job.call_args[1]["fields"]
        assert create_call_fields["sub_agent_id"] == 99

        assert result.sub_agent_id == 99

    @pytest.mark.asyncio
    async def test_auto_creation_sets_automated_type(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """The auto-created sub-agent must have type=automated."""
        from console_backend.models.sub_agent import SubAgentType

        db = AsyncMock()
        created_agent = MagicMock()
        created_agent.id = 77
        mock_sub_agent_service.create_sub_agent.return_value = created_agent
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [created_agent]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1, sub_agent_id=77)

        create_data = ScheduledJobCreate(
            sub_agent_id=None,
            sub_agent_parameters=AutomatedSubAgentConfig(
                name="Auto",
                description="Automated agent",
                model="claude-sonnet-4.5",
                system_prompt="Do the thing.",
            ),
            name="Auto Task Job",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=3600,
            prompt="Run",
        )

        await service.create_job(db=db, data=create_data, actor=actor)

        create_call_kwargs = mock_sub_agent_service.create_sub_agent.call_args[1]
        assert create_call_kwargs["data"].type == SubAgentType.AUTOMATED

    @pytest.mark.asyncio
    async def test_auto_creation_with_capability_tier(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """A tier-bound automated config creates a sub-agent with model_tier set (model None),
        so the scheduled agent follows the fleet default instead of a pinnable alias."""
        from console_backend.models.sub_agent import ModelTier

        db = AsyncMock()
        created_agent = MagicMock()
        created_agent.id = 55
        mock_sub_agent_service.create_sub_agent.return_value = created_agent
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [created_agent]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1, sub_agent_id=55)

        create_data = ScheduledJobCreate(
            sub_agent_id=None,
            sub_agent_parameters=AutomatedSubAgentConfig(
                name="Tiered Auto",
                description="Bound to the standard tier",
                model_tier=ModelTier.STANDARD,
                system_prompt="Do the thing.",
            ),
            name="Tiered Job",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=3600,
            prompt="Run",
        )

        await service.create_job(db=db, data=create_data, actor=actor)

        created = mock_sub_agent_service.create_sub_agent.call_args[1]["data"]
        assert created.model_tier == ModelTier.STANDARD
        assert created.model is None  # tier-bound, not a pinned alias


class TestCreateJobAccessControl:
    """create_job() enforces sub_agent access control."""

    @pytest.mark.asyncio
    async def test_raises_when_sub_agent_inaccessible(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """Raises ValueError when referencing a sub-agent the user cannot access."""
        db = AsyncMock()
        # Accessible agents list does NOT include sub-agent 999
        mock_sub_agent_service.get_accessible_sub_agents.return_value = []

        with pytest.raises(ValueError, match="Access denied"):
            await service.create_job(
                db=db,
                data=_make_interval_create(sub_agent_id=999),
                actor=actor,
            )

    @pytest.mark.asyncio
    async def test_succeeds_when_sub_agent_accessible(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """No exception when referencing an accessible sub-agent."""
        db = AsyncMock()
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1, sub_agent_id=42)

        result = await service.create_job(
            db=db,
            data=_make_interval_create(sub_agent_id=42),
            actor=actor,
        )
        assert result.sub_agent_id == 42

    @pytest.mark.asyncio
    async def test_raises_when_delivery_channel_missing(
        self,
        service: SchedulerService,
        mock_sub_agent_service: AsyncMock,
        mock_delivery_channel_repo: AsyncMock,
        actor: User,
    ):
        """Raises ValueError (not a DB FK 500) when delivery_channel_id does not exist."""
        db = AsyncMock()
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_delivery_channel_repo.get_channel_by_id.return_value = None

        data = _make_interval_create(sub_agent_id=42)
        data.delivery_channel_id = 999

        with pytest.raises(ValueError, match="Delivery channel 999 not found"):
            await service.create_job(db=db, data=data, actor=actor)

    @pytest.mark.asyncio
    async def test_no_access_check_for_watch_without_sub_agent(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """Watch jobs without sub_agent_id skip access control checks."""
        db = AsyncMock()
        mock_repo.create_job.return_value = 5
        now = datetime.now(timezone.utc)
        watch_job = ScheduledJob(
            id=5,
            user_id=actor.id,
            sub_agent_id=None,
            name="My Watch Job",
            job_type=JobType.WATCH,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=300,
            next_run_at=now + timedelta(minutes=5),
            check_tool="gh_get_pr",
            cel_expr="eq_ci(result.state, 'merged')",
            enabled=True,
            max_failures=3,
            consecutive_failures=0,
            created_at=now,
            updated_at=now,
        )
        mock_repo.get_job.return_value = watch_job

        watch_data = ScheduledJobCreate(
            sub_agent_id=None,
            name="PR Watch Job",
            job_type=JobType.WATCH,
            schedule_kind=ScheduleKind.INTERVAL,
            interval_seconds=300,
            check_tool="gh_get_pr",
            cel_expr="eq_ci(result.state, 'merged')",
        )
        result = await service.create_job(db=db, data=watch_data, actor=actor)

        # get_accessible_sub_agents should NOT have been called (no sub_agent_id to validate)
        mock_sub_agent_service.get_accessible_sub_agents.assert_not_awaited()
        assert result is not None


class TestCreateJobNextRunAt:
    """create_job() correctly computes next_run_at for each schedule kind."""

    @pytest.mark.asyncio
    async def test_interval_next_run_at_is_in_future(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """next_run_at for interval schedule must be after now."""
        db = AsyncMock()
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_repo.create_job.return_value = 1

        before = datetime.now(timezone.utc)
        returned_job = _make_job(job_id=1)
        mock_repo.get_job.return_value = returned_job

        await service.create_job(db=db, data=_make_interval_create(), actor=actor)

        fields = mock_repo.create_job.call_args[1]["fields"]
        assert fields["next_run_at"] is not None
        assert fields["next_run_at"] >= before

    @pytest.mark.asyncio
    async def test_once_job_uses_run_at_as_next_run_at(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """Once-only job sets next_run_at = run_at."""
        db = AsyncMock()
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1)

        run_at = datetime(2027, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
        once_data = ScheduledJobCreate(
            sub_agent_id=42,
            name="Once Only Task",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.ONCE,
            run_at=run_at,
            prompt="Run once",
        )
        await service.create_job(db=db, data=once_data, actor=actor)

        fields = mock_repo.create_job.call_args[1]["fields"]
        assert fields["next_run_at"] == run_at


class TestUpdateJobUnsetSentinel:
    """update_job() uses _UNSET to distinguish 'no change' from 'set to None'."""

    @pytest.mark.asyncio
    async def test_unset_fields_not_included(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """Fields not passed to update_job() are excluded from the update payload."""
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id)
        mock_repo.get_job.return_value = existing_job
        mock_repo.update_job.return_value = None
        mock_repo.get_job.side_effect = [existing_job, _make_job(user_id=actor.id)]

        update_data = ScheduledJobUpdate()  # no fields set
        await service.update_job(db=db, job_id=1, data=update_data, actor=actor)

        fields = mock_repo.update_job.call_args[1]["fields"]
        # Only 'updated_at' should be in the patch; no user-controlled fields
        assert "name" not in fields
        assert "prompt" not in fields
        assert "check_tool" not in fields

    @pytest.mark.asyncio
    async def test_explicit_none_clears_field(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """Passing name=None explicitly sets the field to None in the update."""
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id)
        mock_repo.get_job.return_value = existing_job
        mock_repo.update_job.return_value = None
        mock_repo.get_job.side_effect = [existing_job, _make_job(user_id=actor.id)]

        update_data = ScheduledJobUpdate()
        # We pass name=None explicitly — should be included in fields
        await service.update_job(db=db, job_id=1, data=update_data, actor=actor, name=None)

        fields = mock_repo.update_job.call_args[1]["fields"]
        assert "name" in fields
        assert fields["name"] is None

    @pytest.mark.asyncio
    async def test_update_raises_when_delivery_channel_missing(
        self,
        service: SchedulerService,
        mock_repo: AsyncMock,
        mock_delivery_channel_repo: AsyncMock,
        actor: User,
    ):
        """update_job() validates delivery_channel_id before the DB FK constraint."""
        db = AsyncMock()
        mock_repo.get_job.return_value = _make_job(user_id=actor.id)
        mock_delivery_channel_repo.get_channel_by_id.return_value = None

        with pytest.raises(ValueError, match="Delivery channel 1 not found"):
            await service.update_job(
                db=db, job_id=1, data=ScheduledJobUpdate(), actor=actor, delivery_channel_id=1
            )
        mock_repo.update_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_clearing_delivery_channel_skips_validation(
        self,
        service: SchedulerService,
        mock_repo: AsyncMock,
        mock_delivery_channel_repo: AsyncMock,
        actor: User,
    ):
        """Setting delivery_channel_id=None (clearing) must not trigger an existence check."""
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id)
        mock_repo.get_job.side_effect = [existing_job, existing_job]
        mock_repo.update_job.return_value = None

        await service.update_job(
            db=db, job_id=1, data=ScheduledJobUpdate(), actor=actor, delivery_channel_id=None
        )

        mock_delivery_channel_repo.get_channel_by_id.assert_not_awaited()
        fields = mock_repo.update_job.call_args[1]["fields"]
        assert fields["delivery_channel_id"] is None

    @pytest.mark.asyncio
    async def test_update_sets_sub_agent_on_watch_after_access_check(
        self,
        service: SchedulerService,
        mock_repo: AsyncMock,
        mock_sub_agent_service: AsyncMock,
        actor: User,
    ):
        """A watch job can be given a sub_agent_id, subject to the access check."""
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id, job_type=JobType.WATCH, sub_agent_id=None)
        mock_repo.get_job.side_effect = [existing_job, existing_job]
        mock_repo.update_job.return_value = None
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]

        await service.update_job(
            db=db, job_id=1, data=ScheduledJobUpdate(), actor=actor, sub_agent_id=42
        )

        mock_sub_agent_service.get_accessible_sub_agents.assert_awaited_once()
        fields = mock_repo.update_job.call_args[1]["fields"]
        assert fields["sub_agent_id"] == 42

    @pytest.mark.asyncio
    async def test_update_clearing_sub_agent_on_watch_skips_access_check(
        self,
        service: SchedulerService,
        mock_repo: AsyncMock,
        mock_sub_agent_service: AsyncMock,
        actor: User,
    ):
        """Setting sub_agent_id=None on a watch clears it back to notify-only."""
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id, job_type=JobType.WATCH)
        mock_repo.get_job.side_effect = [existing_job, existing_job]
        mock_repo.update_job.return_value = None

        await service.update_job(
            db=db, job_id=1, data=ScheduledJobUpdate(), actor=actor, sub_agent_id=None
        )

        mock_sub_agent_service.get_accessible_sub_agents.assert_not_awaited()
        fields = mock_repo.update_job.call_args[1]["fields"]
        assert fields["sub_agent_id"] is None

    @pytest.mark.asyncio
    async def test_update_clearing_sub_agent_on_task_is_rejected(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        """Task jobs require a sub-agent, so clearing it must fail."""
        db = AsyncMock()
        mock_repo.get_job.return_value = _make_job(user_id=actor.id, job_type=JobType.TASK)

        with pytest.raises(ValueError, match="cannot be cleared on a task job"):
            await service.update_job(
                db=db, job_id=1, data=ScheduledJobUpdate(), actor=actor, sub_agent_id=None
            )
        mock_repo.update_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_returns_none_for_other_users_job(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        """update_job() returns None when job belongs to a different user."""
        db = AsyncMock()
        other_user_job = _make_job(user_id="other-user")
        mock_repo.get_job.return_value = other_user_job  # different user

        result = await service.update_job(db=db, job_id=1, data=ScheduledJobUpdate(), actor=actor)
        assert result is None


class TestUpdateJobScheduleSwitch:
    """Switching schedule_kind must clear the stale schedule columns and recompute next_run_at.

    The DB check constraint scheduled_jobs_schedule_config requires exactly one
    schedule config; leaving e.g. cron_expr set while switching to 'once' used to
    surface as an IntegrityError 500 (prod incident 2026-08-03, job 4).
    """

    @pytest.mark.asyncio
    async def test_cron_to_once_clears_cron_expr(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        db = AsyncMock()
        existing_job = _make_job(
            user_id=actor.id, schedule_kind=ScheduleKind.CRON, interval_seconds=None
        )
        existing_job.cron_expr = "0 8 * * *"
        mock_repo.get_job.side_effect = [existing_job, existing_job]
        mock_repo.update_job.return_value = None

        run_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        data = ScheduledJobUpdate(schedule_kind=ScheduleKind.ONCE, run_at=run_at)
        await service.update_job(db=db, job_id=1, data=data, actor=actor)

        fields = mock_repo.update_job.call_args[1]["fields"]
        assert fields["schedule_kind"] == "once"
        assert fields["run_at"] == run_at
        assert fields["cron_expr"] is None
        assert fields["interval_seconds"] is None
        # once-jobs run at run_at itself
        assert fields["next_run_at"] == run_at

    @pytest.mark.asyncio
    async def test_interval_to_cron_clears_interval(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id)  # interval job
        mock_repo.get_job.side_effect = [existing_job, existing_job]
        mock_repo.update_job.return_value = None

        data = ScheduledJobUpdate(schedule_kind=ScheduleKind.CRON, cron_expr="0 8 * * *")
        await service.update_job(db=db, job_id=1, data=data, actor=actor)

        fields = mock_repo.update_job.call_args[1]["fields"]
        assert fields["schedule_kind"] == "cron"
        assert fields["cron_expr"] == "0 8 * * *"
        assert fields["interval_seconds"] is None
        assert fields["run_at"] is None
        assert fields["next_run_at"] > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_switch_to_once_without_run_at_raises(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id)  # interval job, run_at unset
        mock_repo.get_job.return_value = existing_job

        with pytest.raises(ValueError, match="'once' requires run_at"):
            await service.update_job(
                db=db, job_id=1, data=ScheduledJobUpdate(schedule_kind=ScheduleKind.ONCE), actor=actor
            )
        mock_repo.update_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_switch_to_cron_without_expr_raises(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id)  # interval job, cron_expr unset
        mock_repo.get_job.return_value = existing_job

        with pytest.raises(ValueError, match="'cron' requires cron_expr"):
            await service.update_job(
                db=db, job_id=1, data=ScheduledJobUpdate(schedule_kind=ScheduleKind.CRON), actor=actor
            )
        mock_repo.update_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_schedule_only_patch_recomputes_next_run_at(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        """Regression: the old code checked for schedule fields in the update payload
        BEFORE adding them, so next_run_at was never recomputed on a schedule change."""
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id)  # interval 3600s
        mock_repo.get_job.side_effect = [existing_job, existing_job]
        mock_repo.update_job.return_value = None

        before = datetime.now(timezone.utc)
        await service.update_job(
            db=db, job_id=1, data=ScheduledJobUpdate(interval_seconds=120), actor=actor
        )

        fields = mock_repo.update_job.call_args[1]["fields"]
        assert fields["interval_seconds"] == 120
        assert "next_run_at" in fields
        assert timedelta(seconds=100) < fields["next_run_at"] - before < timedelta(seconds=140)

    @pytest.mark.asyncio
    async def test_non_schedule_patch_leaves_schedule_untouched(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        db = AsyncMock()
        existing_job = _make_job(user_id=actor.id)
        mock_repo.get_job.side_effect = [existing_job, existing_job]
        mock_repo.update_job.return_value = None

        await service.update_job(
            db=db, job_id=1, data=ScheduledJobUpdate(), actor=actor, name="renamed"
        )

        fields = mock_repo.update_job.call_args[1]["fields"]
        for f in ("schedule_kind", "cron_expr", "interval_seconds", "run_at", "next_run_at"):
            assert f not in fields


class TestJobTimezone:
    """Jobs snapshot the owner's settings timezone and evaluate cron in it."""

    @staticmethod
    def _cron_create(timezone_name: str | None = None) -> ScheduledJobCreate:
        return ScheduledJobCreate(
            sub_agent_id=42,
            name="Morning Priorities",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.CRON,
            cron_expr="0 8 * * *",
            timezone=timezone_name,
            prompt="Summarize",
        )

    @pytest.mark.asyncio
    async def test_create_defaults_timezone_from_user_settings(
        self,
        service: SchedulerService,
        mock_repo: AsyncMock,
        mock_sub_agent_service: AsyncMock,
        mock_user_settings_service: AsyncMock,
        actor: User,
    ):
        db = AsyncMock()
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1)

        await service.create_job(db=db, data=self._cron_create(), actor=actor)

        mock_user_settings_service.get_settings.assert_awaited_once()
        fields = mock_repo.create_job.call_args[1]["fields"]
        assert fields["timezone"] == "Europe/Zurich"

    @pytest.mark.asyncio
    async def test_create_explicit_timezone_skips_settings_lookup(
        self,
        service: SchedulerService,
        mock_repo: AsyncMock,
        mock_sub_agent_service: AsyncMock,
        mock_user_settings_service: AsyncMock,
        actor: User,
    ):
        db = AsyncMock()
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1)

        await service.create_job(db=db, data=self._cron_create("America/New_York"), actor=actor)

        mock_user_settings_service.get_settings.assert_not_awaited()
        fields = mock_repo.create_job.call_args[1]["fields"]
        assert fields["timezone"] == "America/New_York"

    @pytest.mark.asyncio
    async def test_cron_next_run_uses_job_timezone(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """'0 8 * * *' in Europe/Zurich must fire at 08:00 Zurich time, not 08:00 UTC."""
        from zoneinfo import ZoneInfo

        db = AsyncMock()
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1)

        await service.create_job(db=db, data=self._cron_create(), actor=actor)

        next_run_at = mock_repo.create_job.call_args[1]["fields"]["next_run_at"]
        local = next_run_at.astimezone(ZoneInfo("Europe/Zurich"))
        assert (local.hour, local.minute) == (8, 0)

    @pytest.mark.asyncio
    async def test_naive_run_at_is_interpreted_in_job_timezone(
        self, service: SchedulerService, mock_repo: AsyncMock, mock_sub_agent_service: AsyncMock, actor: User
    ):
        """A datetime-local value without offset means local wall-clock, not UTC."""
        from zoneinfo import ZoneInfo

        db = AsyncMock()
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1)

        once_data = ScheduledJobCreate(
            sub_agent_id=42,
            name="Once Local Task",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.ONCE,
            run_at=datetime(2027, 6, 15, 14, 30),  # naive — as the datetime-local input sends it
            prompt="Run once",
        )
        await service.create_job(db=db, data=once_data, actor=actor)

        fields = mock_repo.create_job.call_args[1]["fields"]
        assert fields["run_at"] == datetime(2027, 6, 15, 14, 30, tzinfo=ZoneInfo("Europe/Zurich"))
        assert fields["next_run_at"] == fields["run_at"]

    @pytest.mark.asyncio
    async def test_update_timezone_recomputes_next_run_at(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        from zoneinfo import ZoneInfo

        db = AsyncMock()
        existing_job = _make_job(
            user_id=actor.id, schedule_kind=ScheduleKind.CRON, interval_seconds=None
        )
        existing_job.cron_expr = "0 8 * * *"
        mock_repo.get_job.side_effect = [existing_job, existing_job]
        mock_repo.update_job.return_value = None

        await service.update_job(
            db=db, job_id=1, data=ScheduledJobUpdate(timezone="Asia/Tokyo"), actor=actor
        )

        fields = mock_repo.update_job.call_args[1]["fields"]
        assert fields["timezone"] == "Asia/Tokyo"
        local = fields["next_run_at"].astimezone(ZoneInfo("Asia/Tokyo"))
        assert (local.hour, local.minute) == (8, 0)


class TestResumeJob:
    """resume_job guards: a finished once-job must not silently re-run."""

    @pytest.mark.asyncio
    async def test_resume_completed_once_job_raises(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        now = datetime.now(timezone.utc)
        job = _make_job(user_id=actor.id, schedule_kind=ScheduleKind.ONCE, interval_seconds=None)
        job.run_at = now - timedelta(hours=1)
        job.next_run_at = job.run_at
        job.enabled = False
        mock_repo.get_job.return_value = job

        with pytest.raises(ValueError, match="already run"):
            await service.resume_job(db=AsyncMock(), job_id=1, actor=actor)
        mock_repo.update_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_future_once_job_re_enables(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        now = datetime.now(timezone.utc)
        job = _make_job(user_id=actor.id, schedule_kind=ScheduleKind.ONCE, interval_seconds=None)
        job.run_at = now + timedelta(days=1)
        job.next_run_at = job.run_at
        job.enabled = False
        mock_repo.get_job.return_value = job

        assert await service.resume_job(db=AsyncMock(), job_id=1, actor=actor) is True
        fields = mock_repo.update_job.call_args[1]["fields"]
        assert fields["enabled"] is True

    @pytest.mark.asyncio
    async def test_resume_cron_job_with_invalid_timezone_raises_value_error(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        """An unresolvable stored timezone surfaces as ValueError (→ 400), not KeyError (→ 500)."""
        job = _make_job(user_id=actor.id, schedule_kind=ScheduleKind.CRON, interval_seconds=None)
        job.cron_expr = "0 8 * * *"
        job.timezone = "Zurich"  # migrated verbatim from unvalidated user settings
        job.enabled = False
        mock_repo.get_job.return_value = job

        with pytest.raises(ValueError, match="Unknown IANA timezone"):
            await service.resume_job(db=AsyncMock(), job_id=1, actor=actor)


class TestSettingsTimezoneFallback:
    """Empty or missing settings timezones resolve through DEFAULT_TIMEZONE."""

    @pytest.mark.asyncio
    async def test_empty_settings_timezone_falls_back_to_env_default(
        self,
        service: SchedulerService,
        mock_repo: AsyncMock,
        mock_sub_agent_service: AsyncMock,
        mock_user_settings_service: AsyncMock,
        actor: User,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "Asia/Tokyo")
        # Legacy rows can carry an empty string (predates schema validation).
        mock_user_settings_service.get_settings.return_value = UserSettings(user_id=actor.id, timezone="")
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]
        mock_repo.create_job.return_value = 1
        mock_repo.get_job.return_value = _make_job(job_id=1)

        data = ScheduledJobCreate(
            sub_agent_id=42,
            name="Morning Priorities",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.CRON,
            cron_expr="0 8 * * *",
            prompt="Summarize",
        )
        await service.create_job(db=AsyncMock(), data=data, actor=actor)

        fields = mock_repo.create_job.call_args[1]["fields"]
        assert fields["timezone"] == "Asia/Tokyo"

    @pytest.mark.asyncio
    async def test_invalid_settings_timezone_raises_clean_value_error(
        self,
        service: SchedulerService,
        mock_repo: AsyncMock,
        mock_sub_agent_service: AsyncMock,
        mock_user_settings_service: AsyncMock,
        actor: User,
    ):
        mock_user_settings_service.get_settings.return_value = UserSettings(user_id=actor.id, timezone="Zurich")
        accessible = MagicMock()
        accessible.id = 42
        mock_sub_agent_service.get_accessible_sub_agents.return_value = [accessible]

        data = ScheduledJobCreate(
            sub_agent_id=42,
            name="Morning Priorities",
            job_type=JobType.TASK,
            schedule_kind=ScheduleKind.CRON,
            cron_expr="0 8 * * *",
            prompt="Summarize",
        )
        with pytest.raises(ValueError, match="settings timezone"):
            await service.create_job(db=AsyncMock(), data=data, actor=actor)


class TestGetRun:
    """get_run(): single-run lookup for conversation adoption — must stay
    user-scoped and reachable for runs older than the list_runs cap."""

    @pytest.mark.asyncio
    async def test_returns_run_for_owned_job(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        db = AsyncMock()
        mock_repo.get_job.return_value = _make_job(user_id=actor.id)
        sentinel_run = object()
        mock_repo.get_run.return_value = sentinel_run

        result = await service.get_run(db=db, job_id=1, run_id=42, user_id=actor.id)

        assert result is sentinel_run
        mock_repo.get_run.assert_awaited_once_with(db, 1, 42)

    @pytest.mark.asyncio
    async def test_returns_none_for_other_users_job(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        db = AsyncMock()
        mock_repo.get_job.return_value = _make_job(user_id="other-user")

        result = await service.get_run(db=db, job_id=1, run_id=42, user_id=actor.id)

        assert result is None
        mock_repo.get_run.assert_not_awaited()
