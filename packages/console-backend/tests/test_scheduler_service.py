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
from console_backend.models.user import User, UserRole, UserStatus
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
def service(mock_repo, mock_sub_agent_service) -> SchedulerService:
    s = SchedulerService()
    s.set_repository(mock_repo)
    s.set_sub_agent_service(mock_sub_agent_service)
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
        call_kwargs = mock_sub_agent_service.create_sub_agent.call_args

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
            condition_expr="$.state",
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
            condition_expr="$.state",
            expected_value="merged",
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
    async def test_update_returns_none_for_other_users_job(
        self, service: SchedulerService, mock_repo: AsyncMock, actor: User
    ):
        """update_job() returns None when job belongs to a different user."""
        db = AsyncMock()
        other_user_job = _make_job(user_id="other-user")
        mock_repo.get_job.return_value = other_user_job  # different user

        result = await service.update_job(db=db, job_id=1, data=ScheduledJobUpdate(), actor=actor)
        assert result is None
