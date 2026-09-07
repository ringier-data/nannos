"""Tests for the generated-job draft.

The draft is derived from ScheduledJobCreate so the two cannot drift; these tests pin
that relationship down, plus the leniency that makes a partly-wrong generation usable
instead of a 500.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from console_backend.models.scheduled_job import (
    JobType,
    ScheduledJobCreate,
    ScheduledJobDraft,
    ScheduleKind,
)
from console_backend.routers.scheduler_router import (
    _agent_choices,
    _build_draft,
    _coerce_enum,
    _coerce_id,
)


class TestDraftMirrorsCreate:
    def test_every_create_field_is_draftable(self):
        # The point of deriving it: a new job field becomes generatable for free, and
        # cannot be misspelled here into something create silently ignores.
        assert set(ScheduledJobDraft.model_fields) == set(ScheduledJobCreate.model_fields)

    def test_nothing_is_required(self):
        # A generated draft is allowed to be incomplete — a fabricated schedule would
        # be worse than an empty one.
        assert ScheduledJobDraft().model_dump(exclude_none=True) == {}

    def test_constraints_are_relaxed(self):
        # name has min_length=1 on create; a draft has to survive a bad value so the
        # caller can see and fix it.
        with pytest.raises(ValidationError):
            ScheduledJobCreate(
                name="", job_type=JobType.TASK, schedule_kind=ScheduleKind.INTERVAL,
                interval_seconds=300, sub_agent_id=1,
            )
        assert ScheduledJobDraft(name="").name == ""

    def test_a_draft_can_be_submitted_as_a_create(self):
        draft = ScheduledJobDraft(
            name="External invitees",
            job_type=JobType.WATCH,
            schedule_kind=ScheduleKind.CRON,
            cron_expr="0 7-18 * * 1-5",
            check_tool="gcal_list_events",
            cel_expr="size(result.events) > 0",
        )
        # The field names line up, so applying a draft needs no mapping layer.
        created = ScheduledJobCreate(**draft.model_dump(exclude_none=True))
        assert created.check_tool == "gcal_list_events"


class TestBuildDraft:
    def test_one_unusable_value_does_not_lose_the_rest(self):
        draft = _build_draft(
            {
                "name": "Sync watch",
                "check_tool": "naonous_get_campaign",
                "run_at": "not a date",  # unusable
                "interval_seconds": "also not a number",  # unusable
            }
        )
        assert draft.name == "Sync watch"
        assert draft.check_tool == "naonous_get_campaign"
        assert draft.run_at is None
        assert draft.interval_seconds is None

    def test_numeric_strings_are_accepted(self):
        # Models commonly answer with "3600"; rejecting that would be pedantic.
        assert _build_draft({"interval_seconds": "3600"}).interval_seconds == 3600

    def test_nones_are_dropped(self):
        assert _build_draft({"name": None, "check_tool": "t"}).model_dump(exclude_none=True) == {
            "check_tool": "t"
        }


class TestCoercion:
    def test_invented_enums_are_discarded(self):
        assert _coerce_enum(JobType, "wa7ch") is None
        assert _coerce_enum(ScheduleKind, None) is None
        assert _coerce_enum(JobType, "WATCH") == JobType.WATCH

    def test_ids_outside_the_offered_set_are_discarded(self):
        # Picking a sub-agent the user cannot reach would be a quiet authorization hole.
        assert _coerce_id(7, {1, 2, 3}) is None
        assert _coerce_id(2, {1, 2, 3}) == 2
        assert _coerce_id("2", {1, 2, 3}) == 2
        assert _coerce_id("nonsense", {1, 2, 3}) is None
        assert _coerce_id(None, {1, 2, 3}) is None


class TestAgentChoices:
    """What the generator is offered as sub-agents.

    This path was reachable only through the live endpoint and went out with an
    AttributeError: a sub-agent's description lives on its config version, not on the
    agent itself, and that version is a LEFT JOIN so it can be missing entirely.
    """

    @staticmethod
    def _agent(name: str, description: str | None, *, with_version: bool = True) -> SimpleNamespace:
        version = SimpleNamespace(description=description) if with_version else None
        return SimpleNamespace(id=abs(hash(name)) % 1000, name=name, config_version=version)

    def test_the_description_comes_from_the_config_version(self):
        choices = _agent_choices([self._agent("triage", "Triages failed syncs")])
        assert choices[0]["description"] == "Triages failed syncs"

    def test_an_agent_without_a_config_version_is_still_offered(self):
        choices = _agent_choices([self._agent("fresh", None, with_version=False)])
        assert choices == [{"id": choices[0]["id"], "name": "fresh", "description": ""}]

    def test_a_null_description_becomes_empty(self):
        assert _agent_choices([self._agent("x", None)])[0]["description"] == ""

    def test_the_voice_agent_is_not_offered(self):
        # It is dispatched through a separate path, not chosen as a job's agent.
        choices = _agent_choices([self._agent("voice-agent", "Calls people"), self._agent("ok", "y")])
        assert [c["name"] for c in choices] == ["ok"]

    def test_long_descriptions_are_truncated_for_the_prompt(self):
        assert len(_agent_choices([self._agent("x", "y" * 500)])[0]["description"]) == 200


class TestSchedulableSubAgents:
    """One definition of which sub-agents a job may run.

    QA hit this: the AI fill picked an agent the picker then showed as "not in your
    list". The offer was built with is_admin=True while create_job validates without it,
    so an administrator could be handed an agent their own submit would reject.
    """

    @pytest.mark.asyncio
    async def test_the_offer_is_not_admin_aware(self):
        from console_backend.services.scheduler_service import SchedulerService

        sub_agent_service = AsyncMock()
        sub_agent_service.get_accessible_sub_agents = AsyncMock(return_value=[])
        service = SchedulerService()
        service.set_sub_agent_service(sub_agent_service)

        await service.schedulable_sub_agents(AsyncMock(), "user-1")

        # A scheduled job runs as its owner, so being an admin must not widen it.
        _, kwargs = sub_agent_service.get_accessible_sub_agents.await_args
        assert "is_admin" not in kwargs or kwargs["is_admin"] is False

    @pytest.mark.asyncio
    async def test_create_validates_against_the_same_set(self):
        from console_backend.services import scheduler_service as module

        source = inspect.getsource(module.SchedulerService.create_job)
        # Calling get_accessible_sub_agents directly here is how the two drifted apart.
        assert "schedulable_sub_agents" in source
        assert "get_accessible_sub_agents" not in source
