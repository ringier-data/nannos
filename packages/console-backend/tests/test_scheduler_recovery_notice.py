"""The notice sent when recovery is exhausted.

Delivered by dispatching an ephemeral notification-only job to agent-runner rather
than by posting to the delivery channel from here — see
docs/adr/0007-interrupted-runs-get-one-fresh-attempt.md.
"""

from unittest.mock import AsyncMock, patch

import pytest
from console_backend.models.scheduled_job import JobRunStatus, RunTrigger, ScheduledJob
from console_backend.repositories.delivery_channel_repository import DeliveryChannelRepository
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from console_backend.services.scheduler_engine import SchedulerEngine
from console_backend.services.scheduler_token_service import SchedulerTokenService
from tests.scheduler_helpers import make_job

_DISPATCH = "console_backend.services.scheduler_engine.dispatch_streaming"


def _make_job(delivery_channel_id: int | None = 7) -> ScheduledJob:
    return make_job(job_id=11, name="Morning digest", delivery_channel_id=delivery_channel_id)


def _make_engine(*, channel: dict | None) -> SchedulerEngine:
    delivery_channel_repo = AsyncMock(spec=DeliveryChannelRepository)
    delivery_channel_repo.get_channel_for_dispatch.return_value = channel
    token_service = AsyncMock(spec=SchedulerTokenService)
    token_service.get_access_token.return_value = "token-xyz"

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False

    return SchedulerEngine(
        repo=AsyncMock(spec=ScheduledJobRepository),
        delivery_channel_repo=delivery_channel_repo,
        token_service=token_service,
        agent_runner_url="http://agent-runner:8000",
        db_session_factory=lambda: session,
    )


_CHANNEL = {
    "webhook_url": "https://slack.example/hook",
    "secret": "s3cret",
    "message_formatting": "slack",
}


class TestRecoveryNotice:
    @pytest.mark.asyncio
    async def test_dispatches_a_notification_only_job(self):
        """No sub_agent_id, so agent-runner delivers the text and runs no agent."""
        engine = _make_engine(channel=_CHANNEL)

        with patch(_DISPATCH, new=AsyncMock(return_value={})) as dispatch:
            await engine._notify_recovery_exhausted(_make_job())

        dispatch.assert_awaited_once()
        kwargs = dispatch.call_args[1]
        assert "sub_agent_id" not in kwargs["metadata"], (
            "a sub_agent_id would make the runner execute an agent for a one-sentence notice"
        )
        assert "scheduled_job_run_id" not in kwargs["metadata"], (
            "the notice is not a run: a run row would go stale, be swept by the healer, "
            "and earn the job another retry"
        )
        assert kwargs["push_config"] == {"url": _CHANNEL["webhook_url"], "token": _CHANNEL["secret"]}
        assert kwargs["metadata"]["messageFormatting"] == "slack"
        assert kwargs["timeout_read"] < 300.0, "nothing is computed; do not wait like an agent is working"
        assert "Morning digest" in kwargs["parts"][0]["text"]

    @pytest.mark.asyncio
    async def test_an_unreachable_runner_is_logged_not_raised(self):
        """The notice is best-effort: the run row already carries the truth."""
        engine = _make_engine(channel=_CHANNEL)

        with patch(_DISPATCH, new=AsyncMock(side_effect=RuntimeError("connection refused"))):
            await engine._notify_recovery_exhausted(_make_job())  # must not raise

    @pytest.mark.asyncio
    async def test_no_channel_means_no_dispatch(self):
        engine = _make_engine(channel=None)

        with patch(_DISPATCH, new=AsyncMock()) as dispatch:
            await engine._notify_recovery_exhausted(_make_job(delivery_channel_id=None))

        dispatch.assert_not_awaited()


class TestWhenTheNoticeIsOwed:
    """Only the terminal case speaks: an absorbed interruption is not news."""

    @staticmethod
    def _notice_due_at(engine: SchedulerEngine) -> object:
        return engine._repo.complete_run.call_args.kwargs["notice_due_at"]

    @pytest.mark.asyncio
    async def test_interrupted_retry_records_the_debt(self):
        """Recorded, not delivered: the agent is mid-restart and this process may be going
        away. Recorded in the same write as the run's outcome, so a death between the two
        cannot leave a lost run that owes nothing."""
        engine = _make_engine(channel=_CHANNEL)
        engine._notify_recovery_exhausted = AsyncMock()

        await engine._finalize(run_id=5, job=_make_job(), status=JobRunStatus.INTERRUPTED, trigger=RunTrigger.RETRY)

        assert engine._repo.complete_run.call_args.kwargs["run_id"] == 5
        assert self._notice_due_at(engine) is not None
        # Delivery belongs to the sweep.
        engine._notify_recovery_exhausted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_first_interruption_owes_nothing(self):
        """It earned a retry; the user hears nothing unless that one is lost too."""
        engine = _make_engine(channel=_CHANNEL)

        await engine._finalize(run_id=5, job=_make_job(), status=JobRunStatus.INTERRUPTED, trigger=RunTrigger.SCHEDULED)

        assert self._notice_due_at(engine) is None

    @pytest.mark.asyncio
    async def test_a_plain_failure_owes_nothing(self):
        """A failed job already reports itself through the normal delivery path."""
        engine = _make_engine(channel=_CHANNEL)

        await engine._finalize(run_id=5, job=_make_job(), status=JobRunStatus.FAILED, trigger=RunTrigger.RETRY)

        assert self._notice_due_at(engine) is None


class TestNoticeSweep:
    """Delivery is whoever ticks next — the process that recorded the debt may be gone."""

    @pytest.mark.asyncio
    async def test_delivered_notice_is_cleared(self):
        engine = _make_engine(channel=_CHANNEL)
        engine._repo.claim_due_notices.return_value = [{"run_id": 5, "job_id": 11}]
        engine._repo.get_job.return_value = _make_job()
        engine._notify_recovery_exhausted = AsyncMock(return_value=True)

        await engine._deliver_due_notices()

        engine._repo.clear_notice.assert_awaited_once()
        assert engine._repo.clear_notice.call_args[0][1] == 5

    @pytest.mark.asyncio
    async def test_a_failed_attempt_leaves_the_debt_standing(self):
        """Claiming already pushed the due time out, so it is simply tried again later."""
        engine = _make_engine(channel=_CHANNEL)
        engine._repo.claim_due_notices.return_value = [{"run_id": 5, "job_id": 11}]
        engine._repo.get_job.return_value = _make_job()
        engine._notify_recovery_exhausted = AsyncMock(return_value=False)

        await engine._deliver_due_notices()

        engine._repo.clear_notice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_deleted_job_cancels_the_debt(self):
        """Nobody left to tell; the obligation must not outlive its subject."""
        engine = _make_engine(channel=_CHANNEL)
        engine._repo.claim_due_notices.return_value = [{"run_id": 5, "job_id": 11}]
        engine._repo.get_job.return_value = None
        engine._notify_recovery_exhausted = AsyncMock()

        await engine._deliver_due_notices()

        engine._notify_recovery_exhausted.assert_not_awaited()
        engine._repo.clear_notice.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_sweep_failure_does_not_escape(self):
        """The tick must keep dispatching jobs even if notices cannot be delivered."""
        engine = _make_engine(channel=_CHANNEL)
        engine._repo.claim_due_notices.side_effect = RuntimeError("database is down")

        await engine._deliver_due_notices()  # must not raise
