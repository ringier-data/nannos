"""Endpoint tests for the two ways a schedule stops being your own (ADR-0010).

`PATCH /jobs/{id}` treats a trigger field as touched only when it arrives NON-null, so an
explicit `{"schedule_kind": null, "cron_expr": null}` was indistinguishable from sending
nothing: the request returned `200` and changed nothing, which reads as "your own
schedule is cleared" when it is not. A QA pass found it the hard way — an agent set a
schedule, undid it, and left the subscriber with an override they could not remove.

Clearing one is now a real operation with its own route, and the PATCH says so instead of
nodding.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from console_backend.db.session import get_db_session
from console_backend.routers import scheduler_router

USER = SimpleNamespace(id="user-1", sub="sub-1", is_administrator=False)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(scheduler_router.router)
    app.dependency_overrides[scheduler_router.require_auth] = lambda: USER
    app.dependency_overrides[scheduler_router.require_auth_or_bearer_token] = lambda: USER
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.state.scheduler_service = MagicMock()
    return TestClient(app)


def _job(**overrides):
    """Only the fields the response model needs to serialise."""
    base = dict(
        id=7,
        user_id=USER.id,
        definition_id=3,
        owner_user_id="owner-1",
        name="Monday report",
        job_type="watch",
        schedule_kind="cron",
        cron_expr="0 9 * * 1-5",
        timezone="Europe/Zurich",
        trigger_policy="overridable",
        trigger_inherited=True,
        enabled=True,
        max_failures=3,
        consecutive_failures=0,
        destroy_after_trigger=True,
        voice_call=False,
        revision=1,
        subscriber_count=2,
        effective_permission="read",
        activated_by="user",
        next_run_at="2026-09-22T07:00:00+00:00",
        created_at="2026-09-21T00:00:00+00:00",
        updated_at="2026-09-21T00:00:00+00:00",
    )
    base.update(overrides)
    return base


class TestANullScheduleIsNotAClear:
    @pytest.mark.parametrize(
        "body",
        [
            {"schedule_kind": None},
            {"schedule_kind": None, "cron_expr": None},
            {"cron_expr": None, "interval_seconds": None, "run_at": None, "timezone": None},
        ],
    )
    def test_it_is_refused_and_says_what_to_call(self, client, body):
        r = client.patch("/api/v1/scheduler/jobs/7", json=body)

        assert r.status_code == 400
        # The message has to name the route, because the caller may be a model.
        assert "scheduler_follow_default_schedule" in r.json()["detail"]
        client.app.state.scheduler_service.update_job.assert_not_called()

    def test_a_real_schedule_still_goes_through(self, client):
        client.app.state.scheduler_service.update_job = AsyncMock(
            return_value=_job(cron_expr="0 8 * * *", trigger_inherited=False)
        )

        r = client.patch("/api/v1/scheduler/jobs/7", json={"schedule_kind": "cron", "cron_expr": "0 8 * * *"})

        assert r.status_code == 200
        client.app.state.scheduler_service.update_job.assert_awaited_once()

    def test_a_null_outside_the_trigger_is_untouched(self, client):
        """`delivery_channel_id: null` means "in-app only" and must keep working."""
        client.app.state.scheduler_service.update_job = AsyncMock(return_value=_job())

        r = client.patch("/api/v1/scheduler/jobs/7", json={"delivery_channel_id": None})

        assert r.status_code == 200
        client.app.state.scheduler_service.update_job.assert_awaited_once()


class TestEveryDefinitionFieldReachesTheService:
    def test_destroy_after_trigger_is_forwarded(self, client):
        """The route builds the service call by hand, one kwarg per field, and this one was
        missing: the console sent `destroy_after_trigger: false`, the route returned 200
        with the job still `true`, and the checkbox snapped back on every save."""
        client.app.state.scheduler_service.update_job = AsyncMock(return_value=_job(destroy_after_trigger=False))

        r = client.patch("/api/v1/scheduler/jobs/7", json={"destroy_after_trigger": False})

        assert r.status_code == 200
        kwargs = client.app.state.scheduler_service.update_job.await_args.kwargs
        assert kwargs["destroy_after_trigger"] is False

    def test_an_absent_field_stays_unset(self, client):
        client.app.state.scheduler_service.update_job = AsyncMock(return_value=_job())

        client.patch("/api/v1/scheduler/jobs/7", json={"name": "Renamed"})

        kwargs = client.app.state.scheduler_service.update_job.await_args.kwargs
        assert kwargs["destroy_after_trigger"] is scheduler_router._UNSET


class TestFollowDefaultScheduleEndpoint:
    def test_it_returns_the_callers_job_back_on_the_default(self, client):
        client.app.state.scheduler_service.follow_default_schedule = AsyncMock(return_value=_job())

        r = client.post("/api/v1/scheduler/jobs/7/follow-default-schedule")

        assert r.status_code == 200
        assert r.json()["trigger_inherited"] is True

    def test_somebody_elses_job_is_a_404(self, client):
        client.app.state.scheduler_service.follow_default_schedule = AsyncMock(return_value=None)

        assert client.post("/api/v1/scheduler/jobs/7/follow-default-schedule").status_code == 404

    def test_an_unresolvable_timezone_is_a_400_not_a_500(self, client):
        client.app.state.scheduler_service.follow_default_schedule = AsyncMock(
            side_effect=ValueError("Unknown timezone 'Mars/Olympus'")
        )

        r = client.post("/api/v1/scheduler/jobs/7/follow-default-schedule")

        assert r.status_code == 400
        assert "Mars/Olympus" in r.json()["detail"]
