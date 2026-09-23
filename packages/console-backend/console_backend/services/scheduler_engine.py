"""Scheduler engine — tick loop that claims due jobs and dispatches them to agent-runner.

The engine runs inside the agent-console backend process as a background asyncio task.
It owns:
  - Job claiming (FOR UPDATE SKIP LOCKED)
  - User token resolution (KMS → Keycloak refresh)
  - Dispatching to agent-runner via the native a2a-sdk v1.1.0 streaming client
  - Recording outcomes in scheduled_job_runs
  - Advancing or disabling jobs based on results
  - Resuming a run parked on its owner's answer (ADR-0009)
"""

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import text

from ringier_a2a_sdk.cost_tracking.attribution import attribution_scope

from ..repositories.model_defaults_repository import ModelDefaultsRepository
from .llm_gateway import gateway_chat
from .spend_attribution import SERVICE_SCHEDULER, billing_subject
from .watch_evaluator import WatchEvaluator, WatchOutcome
from ..models.delivery_channel import DEFAULT_MESSAGE_FORMATTING
from ..models.notification import NotificationType
from .notification_service import NotificationService
from ..models.scheduled_job import (
    ConditionEvaluation,
    JobRunStatus,
    JobType,
    RunTrigger,
    ScheduledJob,
    ScheduledJobRun,
)
from ..repositories.delivery_channel_repository import DeliveryChannelRepository
from ..repositories.scheduled_job_repository import ScheduledJobRepository, compute_next_run
from ..services.scheduler_token_service import SchedulerTokenService
from ..services.socket_notification_manager import SocketNotificationManager
from ..utils.a2a_dispatch import AgentUnreachable, dispatch_streaming

logger = logging.getLogger(__name__)

# How long a run may go without a heartbeat before the healer calls it interrupted.
#
# The dispatching process refreshes last_seen_at every HEARTBEAT_INTERVAL_SECONDS for as
# long as it holds the run's stream, so this is a miss count, not a guess about how long
# a job should take: a legitimately slow run keeps reporting and is never swept, however
# many hours it takes. Three missed beats is the tolerance for a GC pause, a slow query
# or a brief database blip.
#
# This replaced an age-based bound of 30 minutes, which could only ever be a compromise
# between catching a strand quickly and not shooting a healthy long run. Staleness has no
# such tension, which is also what makes the healer correct with more than one scheduler
# process: _in_flight is only the local view, so age alone cannot tell another process's
# healthy run from an abandoned one.
HEARTBEAT_INTERVAL_SECONDS = 20
STALE_RUN_AFTER_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS

# The bound for runs that carry no heartbeat at all: rows written by a process on the
# release before heartbeats existed, which may still be executing them during a rolling
# deploy. Judging those by the heartbeat window would sweep a healthy run and fire a
# duplicate, so they keep the age-based bound they were written under.
HEARTBEATLESS_RUN_AFTER_SECONDS = 30 * 60

# How long after an interruption the fresh attempt becomes claimable. Long enough that a
# runner still restarting is not immediately handed the same work, short enough that a
# scheduled result is not meaningfully late.
RETRY_DELAY_SECONDS = 60

# Read timeout for the ephemeral notification-only dispatch. Nothing is computed on the
# other side, so the long inter-event timeout that covers a working agent would only make
# an unreachable runner take five minutes to say so.
NOTIFY_TIMEOUT_SECONDS = 20.0

# When the notice that a run was lost becomes deliverable, and how often it is retried.
#
# Not immediately: the loss is detected exactly when the agent is least able to answer.
# If the runner is what died it is restarting right now, and delivery would fail at
# connect — no read timeout helps when there is nothing listening. The first attempt waits
# long enough for it to come back.
#
# Retries are cheap here in a way the job's own retry is not: this is a POST of one
# sentence, not an agent turn, so attempting it again costs nothing worth counting. It is
# still bounded — past NOTICE_GIVE_UP_AFTER_SECONDS the news has stopped being useful, and
# a notifier still trying through a long outage is its own small stampede.
NOTICE_FIRST_ATTEMPT_SECONDS = 90
NOTICE_RETRY_INTERVAL_SECONDS = 300
NOTICE_GIVE_UP_AFTER_SECONDS = 3600


@dataclass(frozen=True)
class DispatchOutcome:
    """What one dispatch to agent-runner reported back.

    A tuple until a parked run needed to carry two more things home (the task the
    answer is addressed to, and the ask itself). Naming them is what keeps
    ``_finalize``'s call sites readable — and a parked run whose task id went
    missing is a job stopped with no way to restart it, so they are not optional
    extras bolted onto a positional return.
    """

    status: JobRunStatus
    result_summary: str | None = None
    error_message: str | None = None
    conversation_id: str | None = None
    #: The non-terminal agent-runner task the answer is sent to. Parked runs only.
    parked_task_id: str | None = None
    #: The extension payload delivered with the ask. Parked runs only.
    parked_payload: dict[str, Any] | None = None


#: How a run parked by the watch CHECK, not by an agent, is marked answerable.
#:
#: ``parked_task_id`` is what makes a parked run answerable everywhere — the schedule
#: hold in ``claim_due_jobs``, ``answerable_parked_run``, the console's badge, the
#: run-now refusal — and for an agent's park it is the agent-runner task the answer is
#: addressed to. A watch whose check tool answered ``need-credentials`` never reached
#: agent-runner, so there is no task; what a resume addresses is the check itself, which
#: is simply run again. Rather than teach five places a second column, the id names
#: what the answer goes to, and ``resume_parked_run`` reads the prefix to tell the two
#: apart. The suffix is the parked run's id, so two parks can never share one.
CHECK_PARK_PREFIX = "watch-check:"


def is_check_park(parked_task_id: str | None) -> bool:
    """Whether *parked_task_id* addresses the watch check rather than an agent-runner task."""
    return bool(parked_task_id) and str(parked_task_id).startswith(CHECK_PARK_PREFIX)


#: What the agent is told when the owner answers. Agent-facing, and deliberately blunt
#: about the DECISION: on the fallback path where a server never routed the DataPart,
#: these words are graded approve/reject/unclear by a classifier before the agent sees
#: them (agent_common.core.hitl_resume.classify_reply), and an unclear verdict costs a
#: whole extra round. Softening them is what makes them unclassifiable. Kept in step with
#: client-slack's authResumeText.
_AUTH_RESUME_TEXT = {
    "approved": "I have completed the authorization. Please retry what needed it and continue.",
    "declined": "I am not going to authorize this. Do not ask again — tell me what you cannot do without it.",
}


class SchedulerEngine:
    """Background tick loop that dispatches scheduled jobs to agent-runner."""

    def __init__(
        self,
        repo: ScheduledJobRepository,
        delivery_channel_repo: DeliveryChannelRepository,
        token_service: SchedulerTokenService,
        agent_runner_url: str,
        db_session_factory: Any,  # async_sessionmaker
        socket_notification_manager: SocketNotificationManager | None = None,
        tick_interval_seconds: int = 30,
        claim_limit: int = 10,
        *,
        agent_access_check: Callable[[Any, str, int], Awaitable[bool]],
    ) -> None:
        self._repo = repo
        # (db, subscriber user id, sub_agent_id) -> may this subscriber run that agent
        # right now (ADR-0010). Required, not defaulted: a construction site that forgot
        # it would silently run revoked agents, so it fails with a TypeError instead.
        # Injected rather than imported so the engine keeps no dependency on the
        # sub-agent service; tests pass an explicit allow-all.
        self._agent_access_check = agent_access_check
        self._delivery_channel_repo = delivery_channel_repo
        self._token_service = token_service
        self._agent_runner_url = agent_runner_url.rstrip("/")
        self._db_session_factory = db_session_factory
        self._socket_notification_manager = socket_notification_manager
        self._tick_interval = tick_interval_seconds
        self._claim_limit = claim_limit
        self._running = False
        self._task: asyncio.Task | None = None
        self._watch_evaluator = WatchEvaluator()
        # Durable, console-side owner notices (a job stopping itself). Constructed here
        # rather than injected: it is stateless and every caller would pass the same one.
        self._notification_service = NotificationService()
        # Runs this process is dispatching right now. The healer must not touch them
        # however long they take — a slow agent is not a stuck run.
        self._in_flight: set[int] = set()

    async def start(self) -> None:
        """Start the background tick loop."""
        if self._running:
            return
        self._running = True
        await self._heal_stuck_runs()
        self._task = asyncio.create_task(self._loop(), name="scheduler-engine")
        logger.info(
            "Scheduler engine started (interval=%ds, claim_limit=%d)",
            self._tick_interval,
            self._claim_limit,
        )

    async def _heal_stuck_runs(self) -> None:
        """Interrupt runs whose dispatcher stopped reporting, and owe each one a retry.

        A run gets stranded when the process is killed mid-dispatch, or when recording
        its outcome fails. Nothing else ever revisits the row: it reads as work in
        progress forever, with no duration and no error.

        Runs this process is still dispatching are excluded outright (_in_flight) so a
        local run is never swept on the strength of a heartbeat that merely lost a race
        with the sweep. For everyone else's, the heartbeat is the evidence.
        """
        try:
            now = datetime.now(timezone.utc)
            async with self._db_session_factory() as db:
                healed = await self._repo.interrupt_stale_runs(
                    db,
                    stale_after_seconds=STALE_RUN_AFTER_SECONDS,
                    heartbeatless_after_seconds=HEARTBEATLESS_RUN_AFTER_SECONDS,
                    exclude_run_ids=list(self._in_flight),
                    retry_at=now + timedelta(seconds=RETRY_DELAY_SECONDS),
                    notice_due_at=now + timedelta(seconds=NOTICE_FIRST_ATTEMPT_SECONDS),
                )
                await db.commit()
            if healed:
                logger.warning(
                    "Interrupted %d run(s) whose dispatcher stopped reporting: %s",
                    len(healed),
                    healed,
                )
        except Exception:
            logger.exception("Failed to sweep stale runs")

    async def _notify_recovery_exhausted(self, job: ScheduledJob) -> bool:
        """Tell the user a run was lost, after the retry was interrupted too.

        Returns whether the debt is settled — delivered, or owed to nobody. False means
        the attempt failed and the notice stays owed for a later round.

        Dispatched as an ephemeral notification-only job: an A2A message carrying the
        text and the job's push config but no ``sub_agent_id``, which agent-runner
        delivers without running an agent (see its ``if sub_agent_id:`` branch). This is
        the same path a notification-only watch already takes, and using it keeps the
        number of processes that post to a delivery channel at three — posting from here
        would duplicate the A2A envelope and its token contract across every receiver.

        That it needs the runner is deliberate and survivable: the runner restarts in
        seconds, the notice carries no tools, no context and no sandbox, so it lives
        where the job that died did not.

        Deliberately outside the run machinery it reports on. It records no run, and it
        is never retried. A notify dispatch that created a run row would go stale, be
        swept by the healer, and earn the job another retry — a loop built out of the
        recovery mechanism. Every failure here is logged and dropped: the run row already
        carries the truth, and a notifier that retries during an incident is how one
        unhealthy process becomes a stampede.
        """
        # Plain text, like every other notification the scheduler writes itself: it
        # goes out verbatim on whichever channel the job notifies, and Slack renders
        # Markdown literally.
        delivered = await self.send_plain_notice(
            job,
            f"'{job.name}' could not run. The process handling it stopped before it finished, "
            f"twice in a row, so there is no result for this run. The schedule is unchanged and "
            f"the next run will go ahead as normal.",
            what="recovery notice",
        )
        if delivered:
            logger.info("Job %d: told the user the run was lost", job.id)
        return delivered

    async def send_plain_notice(
        self, job: ScheduledJob, text_body: str, *, what: str = "notice", with_provenance: bool = True
    ) -> bool:
        """Post one line of plain text to a job's own delivery channel, under the
        SUBSCRIBER's identity, running no agent.

        The scheduler's only way of reaching a person where their job's results land. A
        console notification says a thing happened; this says it where they are. Returns
        whether the message is settled — delivered, or owed to nobody because the job
        notifies no channel. False means the attempt failed.
        """
        if job.delivery_channel_id is None:
            logger.info("Job %d has no delivery channel; %s not sent", job.id, what)
            return True

        try:
            async with self._db_session_factory() as db:
                channel = await self._delivery_channel_repo.get_channel_for_dispatch(db, job.delivery_channel_id)
                if not channel:
                    logger.warning("Job %d: delivery channel %d is gone", job.id, job.delivery_channel_id)
                    return True
                access_token = await self._token_service.get_access_token(db, job.user_id)

            # No sub_agent_id: the runner delivers and runs nothing. No
            # scheduled_job_run_id either — this notice is not a run.
            metadata = self._base_metadata(job)
            metadata["messageFormatting"] = self._message_formatting(channel)
            if not with_provenance:
                # A notice that already explains itself — the activation DM says in
                # so many words why this job now runs for this person.
                metadata["scheduled_job_provenance"] = None
            await dispatch_streaming(
                agent_url=self._agent_runner_url,
                access_token=access_token,
                parts=[{"kind": "text", "text": text_body}],
                metadata=metadata,
                push_config=self._push_config(channel),
                # Nothing is being computed, so the long inter-event timeout that covers a
                # working agent would only make a dead runner take five minutes to admit it.
                timeout_read=NOTIFY_TIMEOUT_SECONDS,
            )
            return True
        except Exception:
            logger.warning("Job %d: could not deliver the %s", job.id, what, exc_info=True)
            return False

    async def _notify_job_paused(self, job: ScheduledJob, reason: str | None, run_id: int) -> None:
        """Record, durably, that this job has stopped running and why.

        Best-effort by construction: the job state and the run record are already
        committed, and failing to write a notification must not undo them or fail the
        tick. A lost notice is worse than none only if it is also silent here, so it is
        logged at error level.
        """
        try:
            async with self._db_session_factory() as db:
                await self._notification_service.create_notification(
                    db=db,
                    user_id=job.user_id,
                    notification_type=NotificationType.SCHEDULED_JOB_PAUSED,
                    title=f"Scheduled job paused: {job.name}",
                    message=(
                        reason
                        or "The job was stopped after repeated failures. Check its run history, "
                        "fix what is failing, and resume it."
                    ),
                    metadata={"job_id": job.id, "run_id": run_id},
                )
                await db.commit()
            logger.info("Job %d auto-paused; notified owner %s", job.id, job.user_id)
        except Exception:
            logger.exception("Job %d auto-paused but its owner could not be notified", job.id)

    async def _deliver_due_notices(self) -> None:
        """Deliver the notices owed to users whose runs were lost for good.

        Claiming pushes each notice's due time forward, so an attempt happens once per
        round across every scheduler, and a failed one is simply tried again later.
        Whoever ticks next does the work — which is the point: the process that recorded
        the debt may be the one that went away.
        """
        try:
            now = datetime.now(timezone.utc)
            async with self._db_session_factory() as db:
                due = await self._repo.claim_due_notices(
                    db,
                    next_attempt_at=now + timedelta(seconds=NOTICE_RETRY_INTERVAL_SECONDS),
                    give_up_before=now - timedelta(seconds=NOTICE_GIVE_UP_AFTER_SECONDS),
                    limit=self._claim_limit,
                )
                await db.commit()

            for item in due:
                async with self._db_session_factory() as db:
                    job = await self._repo.get_job(db, item["job_id"])
                    # A deleted job settles the debt too: there is nobody left to tell,
                    # and the obligation should not outlive its subject.
                    settled = job is None or await self._notify_recovery_exhausted(job)
                    if settled:
                        await self._repo.clear_notice(db, item["run_id"])
                        await db.commit()
        except Exception:
            logger.exception("Failed to deliver recovery notices")

    async def _heartbeat(self, run_id: int) -> None:
        """Report this process alive for *run_id* until cancelled.

        Runs for as long as the dispatch holds the agent's stream. Losing a beat is
        survivable — the healer tolerates several — so a failed write is logged at debug
        and the loop continues rather than taking the dispatch down with it.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                async with self._db_session_factory() as db:
                    await self._repo.touch_run(db, run_id)
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Heartbeat failed for run %s", run_id, exc_info=True)

    async def stop(self) -> None:
        """Stop the background tick loop gracefully."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler engine stopped")

    async def run_job_now(self, job: ScheduledJob, run_id: int | None = None) -> None:
        """Immediately dispatch a job outside the normal tick loop.

        Bypasses the claim mechanism — use only for on-demand test runs triggered
        by a user.  The execution is identical to a regular scheduled dispatch:
        offline-token resolution, A2A call to agent-runner, webhook delivery, and
        run-record creation — except that an interrupted manual run earns no retry.
        The user is present and can press again, and reviving a test press through
        the claim path would turn it into a scheduled execution of the job.

        If run_id is provided (pre-created by the caller, with RunTrigger.MANUAL) the
        engine will skip creating a new run record and use the supplied ID instead.
        """
        logger.info("Manual run-now triggered for job %d by user request", job.id)
        await self._dispatch_job(job, run_id=run_id, trigger=RunTrigger.MANUAL)

    async def resume_parked_run(
        self,
        job: ScheduledJob,
        parked_run: ScheduledJobRun,
        decision: str,
        reply_to: dict[str, str] | None = None,
        run_id: int | None = None,
    ) -> int:
        """Answer a run parked on its owner, and run it to completion. Returns the new run id.

        The owner pressed a button in the delivery channel (or in the console), and the
        answer arrives here rather than travelling a chat turn. A chat turn is new work:
        the orchestrator's dispatch proposes a fresh task id, the sub-agent's thread is
        parked, and the executor rejects it. This addresses the task that is actually
        waiting.

        Why the scheduler brokers it at all, given the clicking user is present and their
        own client could call agent-runner directly: a resumed run is a run. It is minutes
        of tool calls and model spend that has to be recorded, heartbeated, and swept if
        the process executing it dies. Every one of those is machinery this engine already
        owns, and a client holding the stream on a floating promise owns none of it — an
        agent-runner that died mid-resume would leave no record the run was ever attempted.

        The caller has already CLAIMED the ask (``clear_parked_task``) and written the
        run row this continues under (*run_id*), in one transaction, so this is only ever
        reached once per parked run — which is what stops a second click resuming a task
        that has since gone terminal — and a process that dies before reaching here
        leaves a stale ``running`` row the healer can recover instead of nothing at all.
        *run_id* is optional only for direct callers in tests; the router always supplies
        it.

        *decision* is passed through as the agent sees it. A decline resumes the task too:
        the agent is told to stop and the run closes on its own terms, instead of staying
        parked on a question that has been answered.
        """
        # Not a race gate — the router's conditional ``clear_parked_task`` is, and it runs
        # before this. It reads the PRE-clear snapshot it was handed, so on the production
        # path the task id is always still set here and this never fires; it is a
        # sanity check for a direct caller that assembled a run object by hand. Logged
        # rather than raised: this body runs as a Starlette background task, where an
        # exception escapes into nothing and the run row would be left running forever.
        if parked_run.status != JobRunStatus.AUTH_REQUIRED or not parked_run.parked_task_id:
            logger.error(
                "Refusing to resume run %d of job %d: it is not waiting for an answer",
                parked_run.id,
                job.id,
            )
            if run_id is not None:
                await self._finalize(
                    run_id=run_id,
                    job=job,
                    status=JobRunStatus.FAILED,
                    error_message="This run was not waiting for an answer.",
                    delivered=False,
                    trigger=RunTrigger.RESUMED,
                )
            return run_id or parked_run.id

        if is_check_park(parked_run.parked_task_id):
            return await self._resume_check_park(job, parked_run, decision, run_id)

        heartbeat: asyncio.Task[None] | None = None
        try:
            if run_id is None:
                async with self._db_session_factory() as db:
                    run_id = await self._repo.create_run(db, job.id, trigger=RunTrigger.RESUMED)
                    await db.commit()

            logger.info(
                "Resuming job %d run %d (%s) as run %d on parked task %s",
                job.id,
                parked_run.id,
                decision,
                run_id,
                parked_run.parked_task_id,
            )

            self._in_flight.add(run_id)
            heartbeat = asyncio.create_task(self._heartbeat(run_id), name=f"scheduler-heartbeat-{run_id}")

            async with self._db_session_factory() as db:
                try:
                    access_token = await self._token_service.get_access_token(db, job.user_id)
                except ValueError as e:
                    await self._finalize(
                        run_id=run_id,
                        job=job,
                        status=JobRunStatus.FAILED,
                        error_message=str(e),
                        delivered=False,
                        trigger=RunTrigger.RESUMED,
                        paused_reason="No offline token stored. User must re-grant scheduler consent.",
                    )
                    return run_id

                # A parked run can wait for days; the subscriber's access to the agent is
                # judged when the answer arrives, as on any other dispatch.
                if not await self._subscriber_may_run(db, job, run_id, RunTrigger.RESUMED):
                    return run_id

                owner_sub = await billing_subject(db, job.user_id, context=f"job {job.id}")
                with attribution_scope(user_sub=owner_sub, scheduled_job_id=job.id, service=SERVICE_SCHEDULER):
                    # THIS run's id, not the parked one's: the payload correlates to the
                    # run now in flight, and above all a follow-up ask must point at the
                    # run that is waiting. Sending the parked run's id meant the second
                    # card addressed a run already answered, refused as "no longer
                    # waiting". The task to continue needs nothing from here — it is
                    # derived from the context, which the whole chain shares.
                    _, metadata, push_config = await self._build_message_args(
                        job, run_id, access_token, db
                    )
                    # Opaque to everything in between: the delivery channel put it on the
                    # card and is the only thing that can read it back.
                    if reply_to:
                        metadata["reply_to_message"] = reply_to

            parts = [
                {"kind": "data", "data": {"authorization": {"decision": decision}}},
                {"kind": "text", "text": _AUTH_RESUME_TEXT[decision]},
            ]

            result_data = await dispatch_streaming(
                agent_url=self._agent_runner_url,
                access_token=access_token,
                parts=parts,
                metadata=metadata,
                context_id=parked_run.conversation_id,
                task_id=parked_run.parked_task_id,
                push_config=push_config,
            )

            outcome = self._parse_result(result_data)
            await self._finalize(
                run_id=run_id,
                job=job,
                status=outcome.status,
                result_summary=outcome.result_summary,
                error_message=outcome.error_message,
                conversation_id=outcome.conversation_id,
                parked_task_id=outcome.parked_task_id,
                parked_payload=outcome.parked_payload,
                trigger=RunTrigger.RESUMED,
                delivered=(job.delivery_channel_id is not None),
            )
            return run_id

        except Exception as e:
            if run_id is None:
                raise
            if isinstance(e, AgentUnreachable):
                logger.warning("agent-runner unreachable resuming job %d: %s", job.id, e)
                status, error_message = JobRunStatus.INTERRUPTED, str(e)
            elif isinstance(e, httpx.HTTPStatusError):
                logger.error("agent-runner HTTP error resuming job %d: %s", job.id, e)
                status = JobRunStatus.FAILED
                error_message = f"agent-runner HTTP {e.response.status_code}: {e.response.text[:500]}"
            else:
                logger.exception("Unexpected error resuming job %d", job.id)
                status, error_message = JobRunStatus.FAILED, str(e)
            try:
                await self._finalize(
                    run_id=run_id,
                    job=job,
                    status=status,
                    error_message=error_message,
                    delivered=False,
                    trigger=RunTrigger.RESUMED,
                )
            except Exception:
                logger.exception("Failed to finalize resumed run %s for job %d", run_id, job.id)
            return run_id
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            if run_id is not None:
                self._in_flight.discard(run_id)

    async def _resume_check_park(
        self,
        job: ScheduledJob,
        parked_run: ScheduledJobRun,
        decision: str,
        run_id: int | None,
    ) -> int:
        """Answer a run parked by its watch check (see ``CHECK_PARK_PREFIX``).

        There is no agent-runner task to continue: the check never got past the tool.
        An approval runs the poll again right now, as a RESUMED run — the check is
        re-evaluated with the credential the owner just stored, and the job carries on
        (dispatching if the condition holds) or parks again if the tool still refuses,
        which consumed the previous ask first and so leaves exactly one. A decline
        releases the schedule: the resumed run closes without touching
        ``consecutive_failures`` (a refused credential is not evidence about the job),
        and the next occurrence asks again — the same shape as a declined agent park.

        Takes no ``reply_to``: the notice a check park sends is prose, not a card, so no
        chat client has coordinates to thread a continuation under. When one does, the
        agent branch shows where they go (``reply_to_message`` in the dispatch metadata).
        """
        if run_id is None:
            async with self._db_session_factory() as db:
                run_id = await self._repo.create_run(db, job.id, trigger=RunTrigger.RESUMED)
                await db.commit()

        logger.info(
            "Resuming job %d run %d (%s) as run %d by re-running its check",
            job.id,
            parked_run.id,
            decision,
            run_id,
        )
        if decision == "approved":
            await self._dispatch_job(job, run_id=run_id, trigger=RunTrigger.RESUMED)
            return run_id

        # Logged rather than raised, like the agent branch: this runs as a Starlette
        # background task, where an exception escapes into nothing and the run row would
        # be left running — and the healer would then retry a check the owner just
        # declined, parking it on a fresh ask.
        try:
            await self._finalize(
                run_id=run_id,
                job=job,
                status=JobRunStatus.FAILED,
                error_message=(
                    "You declined the authorization, so this check was not run. The watch will ask "
                    "again on its next occurrence."
                ),
                delivered=False,
                trigger=RunTrigger.RESUMED,
                counts_as_failure=False,
            )
        except Exception:
            logger.exception("Failed to record the declined authorization as run %s of job %d", run_id, job.id)
        return run_id

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Unhandled error in scheduler tick")
            await asyncio.sleep(self._tick_interval)

    async def _tick(self) -> None:
        await self._heal_stuck_runs()
        await self._deliver_due_notices()

        async with self._db_session_factory() as db:
            claimed = await self._repo.claim_due_jobs(db, limit=self._claim_limit)
            await db.commit()

        if not claimed:
            return

        logger.info("Scheduler claiming %d due job(s)", len(claimed))
        tasks = [asyncio.create_task(self._dispatch_job(c.job, trigger=c.trigger)) for c in claimed]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for c, result in zip(claimed, results):
            if isinstance(result, Exception):
                logger.error("Job %d dispatch raised an unhandled exception: %s", c.job.id, result)

    async def _dispatch_job(
        self,
        job: ScheduledJob,
        run_id: int | None = None,
        trigger: RunTrigger = RunTrigger.SCHEDULED,
    ) -> None:
        """Resolve user token, build A2A payload, call agent-runner, record result.

        *trigger* is why this run exists, and decides what its interruption is worth:
        a SCHEDULED run earns one RETRY, a RETRY earns the user a notice, a MANUAL run
        earns neither.
        """
        if run_id is None:
            async with self._db_session_factory() as db:
                run_id = await self._repo.create_run(db, job.id, trigger=trigger)
                await db.commit()

        logger.info("Dispatching job %d (run %d, %s) to agent-runner", job.id, run_id, trigger.value)

        self._in_flight.add(run_id)
        # Report liveness for as long as this dispatch holds the stream, so another
        # scheduler's healer can tell a slow run from an abandoned one.
        heartbeat = asyncio.create_task(self._heartbeat(run_id), name=f"scheduler-heartbeat-{run_id}")
        try:
            # Resolve user access token and build payload in a single DB session
            async with self._db_session_factory() as db:
                try:
                    access_token = await self._token_service.get_access_token(db, job.user_id)
                except ValueError as e:
                    # No stored offline token — auto-pause the job
                    await self._finalize(
                        run_id=run_id,
                        job=job,
                        status=JobRunStatus.FAILED,
                        error_message=str(e),
                        delivered=False,
                        paused_reason="No offline token stored. User must re-grant scheduler consent.",
                        trigger=trigger,
                    )
                    return

                # The SUBSCRIBER's access to the definition's agent, checked at every
                # dispatch because it can be revoked after the definition was shared
                # (ADR-0010). Missing access pauses this one subscription — a durable
                # notice, no failure count, nobody else's subscription touched. Under the
                # old single-owner row this could not happen: the owner chose the agent.
                if not await self._subscriber_may_run(db, job, run_id, trigger):
                    return

                # Who this run bills to, for every gateway call under it. The two LLM calls
                # below (the watch judge, the notification writer) used to run inside the
                # agent, where these same ContextVars carried the owner and the job id for
                # free; moving the decision here in #166 took them out of that context and
                # nothing replaced it, so the proxy's logger dropped their records
                # (`custom_logger._build_record` needs a user_sub) and the spend left
                # `usage_logs` altogether. `scheduled_job_id` is half the point:
                # `usage_repository` classifies by it, so without it this recurring overhead
                # reads as the user's own 'orchestrator' spend — and the scope says
                # `service` outright rather than leaving that to be inferred from an id.
                #
                # A scope, not `set_attribution`: `_tick` dispatches each job in its own
                # task, but `run_job_now` awaits this inline from a request handler, where
                # a job id left set would follow the rest of that request.
                owner_sub = await billing_subject(db, job.user_id, context=f"job {job.id}")
                with attribution_scope(
                    user_sub=owner_sub, scheduled_job_id=job.id, service=SERVICE_SCHEDULER
                ):
                    # Watch jobs: decide here whether anything is happening. A poll that
                    # does not trigger dispatches nothing at all — and knowing the outcome
                    # before dispatch is what lets the trigger choose its target (an agent,
                    # or a phone call).
                    watch_outcome: WatchOutcome | None = None
                    # The ask a check tool answered with, when it did. Parked AFTER this
                    # session closes, like the dispatch below: the park sends a notice over
                    # the network, and a pooled connection must not sit open across it.
                    check_ask: dict[str, Any] | None = None
                    if self._watch_evaluator.can_evaluate(job):
                        watch_outcome = await self._watch_evaluator.evaluate(db, job, access_token)

                        if watch_outcome.auth_ask is not None:
                            # The check tool needs the owner's credential. ADR-0009 parks
                            # the run on that rather than failing it; the agent path does
                            # so through agent-runner's task, this path has no task and
                            # parks here.
                            check_ask = watch_outcome.auth_ask

                        elif watch_outcome.error:
                            await self._finalize(
                                run_id=run_id,
                                job=job,
                                status=JobRunStatus.FAILED,
                                error_message=watch_outcome.error,
                                delivered=False,
                                last_check_result=watch_outcome.check_result,
                                condition_evaluation=watch_outcome.evaluation,
                                trigger=trigger,
                            )
                            return

                        elif not watch_outcome.condition_met:
                            await self._finalize(
                                run_id=run_id,
                                job=job,
                                status=JobRunStatus.CONDITION_NOT_MET,
                                delivered=False,
                                last_check_result=watch_outcome.check_result,
                                condition_evaluation=watch_outcome.evaluation,
                                trigger=trigger,
                            )
                            return

                    if check_ask is None:
                        # Build the A2A message args for agent-runner
                        parts, metadata, push_config = await self._build_message_args(
                            job, run_id, access_token, db, watch_outcome=watch_outcome
                        )

            if check_ask is not None:
                await self._park_on_check_ask(job, run_id, check_ask, trigger)
                return

            # Dispatch to agent-runner via the native a2a-sdk v1.1.0 streaming client. SSE keeps
            # bytes flowing so CloudFront/ALB idle-timeout never fires for long-running jobs.
            result_data = await dispatch_streaming(
                agent_url=self._agent_runner_url,
                access_token=access_token,
                parts=parts,
                metadata=metadata,
                push_config=push_config,
            )

            # Parse execution result from agent-runner response
            outcome = self._parse_result(result_data)

            # Push notification is delivered by the A2A SDK (BasePushNotificationSender)
            # inside agent-runner when pushNotificationConfig is included in the payload.
            await self._finalize(
                run_id=run_id,
                job=job,
                status=outcome.status,
                result_summary=outcome.result_summary,
                error_message=outcome.error_message,
                conversation_id=outcome.conversation_id,
                parked_task_id=outcome.parked_task_id,
                parked_payload=outcome.parked_payload,
                trigger=trigger,
                delivered=(job.delivery_channel_id is not None),
                # From the local evaluation: the scheduler performed the check, so the
                # runner has no reason to echo it back.
                last_check_result=(
                    watch_outcome.check_result if watch_outcome else result_data.get("last_check_result")
                ),
                condition_evaluation=watch_outcome.evaluation if watch_outcome else None,
            )

        except Exception as e:
            # Only the dispatch itself can say the agent died: dispatch_streaming raises
            # AgentUnreachable for that and nothing else does, so a Keycloak or database
            # error on the way there is a failure of this run, not an interruption.
            if isinstance(e, AgentUnreachable):
                logger.warning("agent-runner unreachable for job %d: %s", job.id, e)
                status = JobRunStatus.INTERRUPTED
                error_message = str(e)
            elif isinstance(e, httpx.HTTPStatusError):
                logger.error("agent-runner HTTP error for job %d: %s", job.id, e)
                status = JobRunStatus.FAILED
                error_message = f"agent-runner HTTP {e.response.status_code}: {e.response.text[:500]}"
            else:
                logger.exception("Unexpected error dispatching job %d", job.id)
                status = JobRunStatus.FAILED
                error_message = str(e)
            try:
                await self._finalize(
                    run_id=run_id,
                    job=job,
                    status=status,
                    error_message=error_message,
                    delivered=False,
                    trigger=trigger,
                )
            except Exception:
                logger.exception("Failed to finalize run %s for job %d after dispatch error", run_id, job.id)
        finally:
            # Await the cancellation: the heartbeat may be inside a session commit, and
            # letting it unwind after this dispatch has reported completion makes
            # shutdown non-deterministic.
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            self._in_flight.discard(run_id)

    async def _park_on_check_ask(
        self,
        job: ScheduledJob,
        run_id: int,
        ask: dict[str, Any],
        trigger: RunTrigger,
    ) -> None:
        """Park a watch whose check tool answered ``need-credentials``.

        The run ends ``AUTH_REQUIRED`` carrying the ask, exactly as an agent's park does
        (ADR-0009 decision 4): the schedule is held until the owner answers, nothing is
        counted against ``max_failures``, and the console renders the ask from
        ``parked_payload`` and answers it through the resume endpoint. What differs is
        the id the answer is addressed to — the check, not an agent-runner task (see
        ``CHECK_PARK_PREFIX``).

        A job with a delivery channel is also told there, so the owner who lives in a chat
        client learns that the job has stopped and gets the link (decision 5). It goes as
        a plain notice — not a run: it carries no run id, so the chat client has nothing
        to adopt as this run's result, which keeps the ask un-adoptable as the ADR
        requires — and the outer task it completes is NOT the parked one: the clients
        render prose with a working link rather than a card, and the answer comes back
        through the console. A delivery that fails is logged and the run still parks; the
        ask is recorded on the run either way, and a park nobody was told about is still
        a stopped job the console shows, which beats a failed run counted against the job.
        """
        # Built two calls upstream by need_credentials_ask, which always emits one method;
        # only the URL is optional, since the gateway's payload may lack it.
        tool_name = job.check_tool or "its check tool"
        authorize_url = ask["auth_requirement"]["auth_methods"][0].get("auth_url")
        summary = f"Stopped: '{tool_name}' needs your authorization before this watch can check anything."

        delivered = False
        if job.delivery_channel_id is not None:
            notice = (
                f"The scheduled watch '{job.name}' has stopped: '{tool_name}' needs your authorization "
                "before it can check anything. "
                + (f"Authorize here: {authorize_url} — then " if authorize_url else "Once authorized, ")
                + "confirm in the console and the watch will pick up where it stopped."
            )
            delivered = await self.send_plain_notice(job, notice, what="authorization notice")

        await self._finalize(
            run_id=run_id,
            job=job,
            status=JobRunStatus.AUTH_REQUIRED,
            result_summary=summary,
            delivered=delivered,
            trigger=trigger,
            parked_task_id=f"{CHECK_PARK_PREFIX}{run_id}",
            parked_payload=ask,
        )

    async def _subscriber_may_run(self, db: Any, job: ScheduledJob, run_id: int, trigger: RunTrigger) -> bool:
        """The per-dispatch access check (ADR-0010), shared by every path that dispatches.

        False means the run has been finalised as a pause of this one subscription: a
        durable notice, no failure count, nobody else's subscription touched.
        """
        if job.sub_agent_id is None or await self._agent_access_check(db, job.user_id, job.sub_agent_id):
            return True
        await self._finalize(
            run_id=run_id,
            job=job,
            status=JobRunStatus.FAILED,
            error_message="You no longer have access to the sub-agent this job runs.",
            delivered=False,
            paused_reason="Agent not accessible: you no longer have access to the sub-agent this job runs.",
            trigger=trigger,
            counts_as_failure=False,
        )
        return False

    async def _build_message_args(
        self,
        job: ScheduledJob,
        run_id: int,
        access_token: str,
        db: Any,
        watch_outcome: WatchOutcome | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str] | None]:
        """Build the (message parts, metadata, push_config) for the A2A SDK dispatch.

        `watch_outcome` is set when the condition was already evaluated here, which is the
        normal path for a watch job. It is passed on so agent-runner does not call the
        tool a second time or reach a different verdict than the one that got us here.
        """
        metadata = self._base_metadata(job)
        metadata["scheduled_job_run_id"] = run_id

        # The channel this job notifies, needed twice below: for how it renders text, and
        # as the push target. Fetched once.
        channel: dict[str, Any] | None = None
        if job.delivery_channel_id is not None:
            channel = await self._delivery_channel_repo.get_channel_for_dispatch(db, job.delivery_channel_id)

        if job.sub_agent_id is not None:
            # sub-agent config will be fetched by agent-runner using the sub_agent_id
            metadata["sub_agent_id"] = job.sub_agent_id

        # What the dispatch carries. agent-runner does not know a watch from a task:
        # it runs the sub-agent a job names, or delivers the text it was given.
        if job.job_type.value == "task":
            message_text = job.prompt or "Execute the task you are designed for."
        elif job.sub_agent_id is not None:
            # A triggered watch with an agent: the instruction plus what triggered it,
            # since the agent is expected to act on the result.
            instruction = job.prompt or "Take appropriate action based on the check result."
            result_json = json.dumps((watch_outcome.check_result if watch_outcome else None), default=str)
            message_text = f"Watch condition triggered. {instruction}\n\nCheck result: {result_json}"
        else:
            # A triggered watch that only notifies: the text is the notification, written
            # here when the author left it empty. It used to be written inside the agent
            # run, which is why a notification-only watch needed one at all.
            message_text = job.notification_message or await self._write_notification(job, watch_outcome)

        # Voice-call dispatch: the target becomes the voice-agent, which reads its
        # configuration from a DataPart and injects any TextParts into the live session
        # as context.
        #
        # No job-type distinction: a watch only reaches here once its condition has been
        # evaluated and met, so a call happens because something happened. (While the
        # evaluation lived in agent-runner, dispatch preceded the verdict and this had to
        # be task-only or the phone would have rung on every poll.)
        is_voice_dispatch = False
        if job.voice_call:
            voice_agent_id = await self._resolve_voice_agent_id(db)
            if voice_agent_id is not None:
                metadata["sub_agent_id"] = voice_agent_id
                is_voice_dispatch = True
            else:
                logger.warning("voice_call=True for job %d but voice-agent not found in DB", job.id)

            # VoiceCallRequest: sub_agent_id borrows another agent's configuration,
            # system_prompt is the alternative when there is no agent to borrow from.
            # One of them has to be set, or the call has no direction — and the payload
            # must not be an empty object, which the voice agent rejects outright.
            call_config: dict[str, Any] = {}
            if job.sub_agent_id is not None:
                call_config["sub_agent_id"] = job.sub_agent_id
            else:
                call_config["system_prompt"] = (
                    f"You are calling the user because their scheduled watch '{job.name}' "
                    "triggered. Tell them what happened, using the check result below, "
                    "then answer any questions they have about it."
                )

            parts: list[dict[str, Any]] = [
                {
                    "kind": "data",
                    "data": call_config,
                    "metadata": {"mimeType": "application/json"},
                },
            ]
            if message_text and message_text != "Execute the task you are designed for.":
                parts.append({"kind": "text", "text": message_text})
            if watch_outcome is not None and watch_outcome.check_result:
                # Without this a notification-only watch would call with nothing to
                # report: the message may be empty, and the agent-runner path that
                # writes one is not taken when the target is the voice agent.
                parts.append(
                    {
                        "kind": "text",
                        "text": f"Check result: {json.dumps(watch_outcome.check_result, default=str)[:4000]}",
                    }
                )
        else:
            parts = [{"kind": "text", "text": message_text}]

        # Attach the push notification config (from the channel fetched above) so the A2A
        # SDK registers it for the task and BasePushNotificationSender can deliver it
        # upon completion.  The channel secret is sent as X-A2A-Notification-Token
        # so the webhook receiver can verify ownership of the notification.
        #
        # This is why a triggered watch is dispatched even when it only sends a
        # notification and runs no agent: delivery is the push sender's job, and the
        # payload is an A2A Task envelope that the delivery channels normalise. Posting
        # it from here would duplicate that contract across three receivers.
        push_config = self._push_config(channel) if channel else None

        # How the channel this job notifies renders text, sent under the key an interactive
        # client uses (`messageFormatting`) so agent-runner and the orchestrator apply the
        # same rules whether a human or a cron started the run. Without it a Slack
        # notification arrives as raw Markdown — '### heading', '**bold**' — because
        # nothing downstream rewrites the agent's output.
        #
        # Set after the voice branch, and keyed on whether the dispatch actually became a
        # voice call rather than on job.voice_call: a call renders nothing, but a job that
        # asked for one and found no voice agent falls back to a text dispatch, and that
        # text still lands on the channel and still has to be written for it.
        if not is_voice_dispatch:
            metadata["messageFormatting"] = self._message_formatting(channel)

        return parts, metadata, push_config

    @staticmethod
    def _provenance_line(job: ScheduledJob) -> str | None:
        """Why this person is receiving this, for a job they did not author (ADR-0010).

        None for an unshared job — which is every job until somebody shares one, and the
        reason this is a line appended to a result rather than a field every client had
        to learn to render. A subscriber of a shared job gets one sentence naming the
        owner and, when a group default put it there, that it was not their own doing.
        """
        if job.owner_user_id == job.user_id:
            return None
        owner = job.owner_email or "another user"
        if job.activated_by == "group":
            return f"(You receive this because '{job.name}', shared by {owner}, is a default job of one of your groups.)"
        return f"(You receive this because you subscribed to '{job.name}', shared by {owner}.)"

    @staticmethod
    def _base_metadata(job: ScheduledJob) -> dict[str, Any]:
        """The A2A message metadata every dispatch on behalf of *job* carries."""
        return {
            "scheduled_job_id": job.id,
            # Appended to the delivered result by agent-runner, where every dispatch's
            # output is composed — one seam instead of the same footer in three clients.
            # Absent (None) on the jobs that are nobody else's, which is most of them.
            "scheduled_job_provenance": SchedulerEngine._provenance_line(job),
            # Carried so a notification can name the job in words. An ask especially:
            # "Nannos needs permission" says nothing about which of a user's jobs has
            # stopped, and the id is not something anyone recognises.
            "scheduled_job_name": job.name,
            "job_type": job.job_type.value,
            # The job's IANA timezone, so the runner's tool-less LLM calls
            # (condition eval, notification generation) can be told "now".
            "timezone": job.timezone or None,
        }

    @staticmethod
    def _push_config(channel: dict[str, Any]) -> dict[str, str]:
        """The push-notification target for a delivery channel, as the A2A task registers it."""
        return {"url": channel["webhook_url"], "token": channel["secret"]}

    @staticmethod
    def _message_formatting(channel: dict[str, Any] | None) -> str:
        """How the channel renders text, under the key an interactive client uses."""
        return (channel or {}).get("message_formatting") or DEFAULT_MESSAGE_FORMATTING

    async def _write_notification(self, job: ScheduledJob, outcome: WatchOutcome | None) -> str:
        """Write the notification for a triggered watch whose author left it empty.

        Moved here from agent-runner along with the rest of the decision: the scheduler
        already has the check result, and a notification-only watch was otherwise paying
        for a whole agent run just to have this sentence written.

        Falls back to reporting the raw result. A watch that triggered has something to
        say, so an unreachable model must not turn that into silence.
        """
        check_result = outcome.check_result if outcome else None
        if not check_result:
            return f"The watch '{job.name}' triggered."

        async with self._db_session_factory() as db:
            defaults = await ModelDefaultsRepository().get_all(db)
        model = defaults.get("chat:low") or defaults.get("chat")
        if not model:
            logger.warning("Job %d: no chat model configured, reporting the raw result", job.id)
            return f"The watch '{job.name}' triggered. Result: {json.dumps(check_result, default=str)[:300]}"

        # No markup, deliberately: this text goes out verbatim on whichever channel the job
        # notifies (Slack renders Markdown literally), and one or two sentences lose nothing
        # by being plain. The channel's own rules are applied where a full reply is composed
        # — the sub-agent run, which is told them via the dispatch metadata.
        prompt = (
            "Write the notification a user receives when a scheduled watch triggers. "
            "One or two sentences, factual, highlighting what changed. Plain text only — "
            "no markdown, no bold, no headings, no bullet points. Reply with the "
            "message text only, no preamble.\n\n"
            f"Watch: {job.name}\n"
            f"Result:\n{json.dumps(check_result, indent=2, default=str)[:6000]}"
        )
        try:
            # Thinking off: two sentences of plain text need no reasoning, and on the low
            # tier a reasoning model spends the budget thinking and is cut off mid-sentence
            # — which would then be sent to the person verbatim.
            # Cost attribution comes from the scope `_dispatch_job` opened, not from an
            # argument here — the header is stamped by `_gateway_headers`.
            message = await gateway_chat(prompt, model=model, max_tokens=256, reasoning_effort="none")
            written = message.strip().strip('"')
            if written:
                logger.info("Job %d: wrote notification %r", job.id, written[:100])
                return written
        except Exception:
            logger.warning("Job %d: writing the notification failed", job.id, exc_info=True)
        return f"The watch '{job.name}' triggered. Result: {json.dumps(check_result, default=str)[:300]}"

    async def _resolve_voice_agent_id(self, db: Any) -> int | None:
        """Look up the voice-agent sub_agent_id from the DB (system-owned)."""
        result = await db.execute(
            text(
                "SELECT id FROM sub_agents WHERE name = 'voice-agent' AND owner_user_id = 'system' AND deleted_at IS NULL LIMIT 1"
            )
        )
        row = result.scalar_one_or_none()
        return row

    def _parse_result(self, data: dict[str, Any]) -> DispatchOutcome:
        """Extract structured result fields from agent-runner A2A response.

        Supports two response formats:
        1. A2A Task format: result is a Task object with artifacts containing JSON metadata
        2. Legacy custom format: result.metadata contains the scheduler fields directly
        """
        # JSON-RPC error response (e.g. validation failure) — no "result" key
        if "error" in data and "result" not in data:
            error_msg = data["error"].get("message", "JSON-RPC error")
            return DispatchOutcome(
                status=JobRunStatus.FAILED,
                error_message=f"A2A request error: {error_msg}",
            )

        result = data.get("result", {})
        meta: dict[str, Any] = {}
        conversation_id: str = result.get("contextId")

        # --- A2A Task format (agent-runner using A2AFastAPIApplication) ---
        if result.get("kind") == "task" or "artifacts" in result:
            # Extract metadata from the last artifact's text content (JSON-encoded)
            artifacts = result.get("artifacts", [])
            if artifacts:
                last_artifact = artifacts[-1]
                parts = last_artifact.get("parts", [])
                for part in parts:
                    if isinstance(part, dict) and part.get("kind") == "text":
                        text = part.get("text", "")
                        try:
                            meta = json.loads(text)
                        except (json.JSONDecodeError, ValueError):
                            meta = {"result_summary": text}
                        break
                    elif isinstance(part, dict) and part.get("root", {}).get("kind") == "text":
                        text = part["root"].get("text", "")
                        try:
                            meta = json.loads(text)
                        except (json.JSONDecodeError, ValueError):
                            meta = {"result_summary": text}
                        break

            # Fallback: infer status from task state if not in meta
            if "scheduler_status" not in meta:
                task_status = result.get("status", {})
                task_state = task_status.get("state", "completed")
                if task_state == "failed":
                    meta.setdefault("scheduler_status", "failed")
                elif task_state == "completed":
                    meta.setdefault("scheduler_status", "success")
                elif task_state == "auth_required":
                    # This is the arm that reaches here: a park whose status text did not
                    # parse as JSON (prose, truncation, an older runner) has no
                    # ``scheduler_status`` at all, and defaulting it to success would
                    # record a parked run green, reset ``consecutive_failures`` and drop
                    # the ask. Say what the task state said, and let the parked_task_id
                    # guard below decide — with nothing parsed there is no task id, so it
                    # resolves to a failure, which is the honest reading of a park nobody
                    # can answer.
                    meta.setdefault("scheduler_status", "auth_required")
                else:
                    meta.setdefault("scheduler_status", "success")

        # --- Legacy custom format (old agent-runner without A2A SDK) ---
        else:
            meta = result.get("metadata", {})

        status_str = meta.get("scheduler_status", "success")
        try:
            status = JobRunStatus(status_str)
        except ValueError:
            status = JobRunStatus.SUCCESS

        # A parked run carries the two things needed to reach it again. The task id is
        # load-bearing: without it the run is stopped with nothing to address, so a
        # payload claiming AUTH_REQUIRED without one is not a park at all and is
        # recorded as a failure rather than silently stopping the job forever.
        parked_task_id = meta.get("parked_task_id")
        if status == JobRunStatus.AUTH_REQUIRED and not parked_task_id:
            logger.error("agent-runner reported auth_required with no parked_task_id; treating as failed")
            return DispatchOutcome(
                status=JobRunStatus.FAILED,
                result_summary=meta.get("agent_message"),
                error_message=(
                    "agent-runner reported that authorization is required but named no parked task, "
                    "so there is nothing to resume."
                ),
                conversation_id=conversation_id,
            )

        parked_payload = meta.get("auth_payload")
        return DispatchOutcome(
            status=status,
            result_summary=meta.get("agent_message"),
            error_message=meta.get("error_message"),
            conversation_id=conversation_id,
            parked_task_id=parked_task_id if status == JobRunStatus.AUTH_REQUIRED else None,
            parked_payload=parked_payload if isinstance(parked_payload, dict) else None,
        )

    async def _finalize(
        self,
        run_id: int,
        job: ScheduledJob,
        status: JobRunStatus,
        result_summary: str | None = None,
        error_message: str | None = None,
        conversation_id: str | None = None,
        delivered: bool = False,
        last_check_result: dict | None = None,
        paused_reason: str | None = None,
        condition_evaluation: ConditionEvaluation | None = None,
        trigger: RunTrigger = RunTrigger.SCHEDULED,
        parked_task_id: str | None = None,
        parked_payload: dict[str, Any] | None = None,
        counts_as_failure: bool = True,
    ) -> None:
        """Persist run outcome and advance job state.

        *counts_as_failure* False records a FAILED run without moving
        ``consecutive_failures``: the stop is about the subscriber's standing (an agent
        they can no longer reach), not about the job, and the pause reason says so.

        An interrupted SCHEDULED or RESUMED run earns the job one fresh attempt. The
        marker goes in the database rather than being retried here: the process that
        noticed the interruption is often the one dying, and a retry that dies with it
        is no retry at all. An interrupted RETRY has exhausted recovery and owes the
        user a notice instead. An interrupted MANUAL run earns neither — the user is
        present.

        A RESUMED run earns the attempt for a reason worth stating, because the
        opposite reads as obvious: by the time it dies the credential is stored at
        the gateway, so a fresh run no longer asks for it and recovers the work the
        parked run was trying to do. It does NOT advance ``next_run_at`` — the run it
        continues already did — which is why *next_run_at* is left None for it and
        ``complete_job``'s COALESCE keeps the schedule as written.

        A parked run (AUTH_REQUIRED) earns no attempt at all: no retry conjures a
        credential. It stops the job being claimed until the owner answers, which is
        ``claim_due_jobs``' business, not this one's.
        """
        interrupted = status == JobRunStatus.INTERRUPTED
        now = datetime.now(timezone.utc)
        earns_retry = trigger in (RunTrigger.SCHEDULED, RunTrigger.RESUMED)
        retry_at = now + timedelta(seconds=RETRY_DELAY_SECONDS) if interrupted and earns_retry else None
        # Recovery is exhausted: an interrupted run that was already the retry. Only this
        # terminal case is worth a message — an interruption the system absorbed is not
        # news, and a notice per interruption would be loudest exactly during an incident.
        #
        # Recorded as owed, not delivered here. Sending it now would aim at an agent that
        # is, by construction, in the middle of dying or restarting; and if this process
        # is the one going away, an in-process attempt goes with it. The tick loop
        # delivers it once the dust has settled.
        notice_due_at = (
            now + timedelta(seconds=NOTICE_FIRST_ATTEMPT_SECONDS) if interrupted and trigger == RunTrigger.RETRY else None
        )
        if interrupted:
            consequence = {
                RunTrigger.SCHEDULED: f"retrying at {retry_at.isoformat()}" if retry_at else "",
                RunTrigger.RESUMED: f"retrying at {retry_at.isoformat()}" if retry_at else "",
                RunTrigger.RETRY: "already the retry, giving up and owing the user a notice",
                RunTrigger.MANUAL: "a manual run, not retried",
            }[trigger]
            logger.warning("Run %s of job %d was interrupted (%s); %s", run_id, job.id, error_message, consequence)

        # A resumed run continues one whose occurrence already advanced the schedule, so
        # advancing again would silently skip the next one — it says NOTHING about the
        # schedule instead (``leave_schedule``), rather than re-sending the value it read
        # when the owner clicked.
        #
        # That echo was a lost update. ``job`` is a snapshot taken in the router at click
        # time, and a schedule edit recomputes ``next_run_at`` from the new definition
        # (scheduler_service.update_job), so an owner who edited the schedule while the
        # resumed run was in flight had their new time overwritten by the old one when it
        # finalised — the job then firing on a schedule they had already replaced. Saying
        # nothing leaves whatever the edit computed in place, with no window at all.
        leave_schedule = trigger == RunTrigger.RESUMED
        try:
            next_run_at = (
                None
                if leave_schedule
                else compute_next_run(
                    schedule_kind=job.schedule_kind,
                    cron_expr=job.cron_expr,
                    interval_seconds=job.interval_seconds,
                    run_at=job.run_at,
                    after=datetime.now(timezone.utc),
                    tz=job.timezone,
                )
            )
        except ValueError as e:
            # An unresolvable stored timezone must pause the job: raising here
            # would leave next_run_at in the past, so claim_due_jobs would
            # re-claim and re-execute the job on every tick, forever.
            logger.error("Job %d has an unresolvable timezone %r; pausing it: %s", job.id, job.timezone, e)
            next_run_at = None
            if paused_reason is None:
                paused_reason = f"Invalid timezone {job.timezone!r} — fix the job's timezone and resume it."

        # Advance the job first, in its own transaction, and record the run second.
        #
        # These used to share one transaction with the run record written first, which
        # made the schedule advance depend on the bookkeeping write succeeding. When it
        # failed — a column the deployed schema did not have yet — next_run_at stayed in
        # the past and claim_due_jobs re-claimed the job on every tick, forever, calling
        # the check tool each time. The same trap the timezone branch above guards.
        #
        # Splitting them costs atomicity in one direction only: a crash between the two
        # leaves a run stuck in 'running' for the healer to sweep, while the job itself
        # carries on correctly. The other order risks a tight loop, which is far worse.
        async with self._db_session_factory() as db:
            # Disable watch job if destroy_after_trigger is True and condition was
            # successfully met. Belongs with the job update: both are job state.
            should_disable = (
                job.job_type == JobType.WATCH and job.destroy_after_trigger and status == JobRunStatus.SUCCESS
            )

            if should_disable:
                logger.info(
                    "Job %d: Disabling watch job after successful trigger (destroy_after_trigger=True)",
                    job.id,
                )
                # A system action, no user actor. Per SUBSCRIPTION: the watch fired for
                # this subscriber, so this subscriber's job is done; nobody else's is.
                await self._repo.disable_subscription(db, job.id, "Watch condition met (one-time trigger)")

            # A stop that is about the subscriber's standing, not the job: written as a
            # real pause (enabled = FALSE + reason) so the claim loop leaves it alone —
            # complete_job only flips enabled on the failure threshold, which this must
            # never contribute to.
            if not counts_as_failure and paused_reason:
                await self._repo.disable_subscription(db, job.id, paused_reason)

            enabled_after, reason_after = await self._repo.complete_job(
                db=db,
                subscription_id=job.id,
                status=status if counts_as_failure else JobRunStatus.INTERRUPTED,
                next_run_at=next_run_at,
                retry_at=retry_at,
                last_check_result=last_check_result,
                paused_reason=paused_reason,
                leave_schedule=leave_schedule,
            )
            await db.commit()

        # A job that stopped itself has to say so. Auto-pause is decided inside
        # complete_job from consecutive_failures against max_failures, and until now it
        # happened in silence: the job simply stopped producing, which is the one symptom
        # its owner is least likely to notice. The delivery channel cannot carry this —
        # it is optional, and a job without one is exactly the job whose silence goes
        # unnoticed — so the notice is a durable console notification, which also
        # survives the owner being offline in a way the WebSocket push does not.
        if job.enabled and not enabled_after and not should_disable:
            await self._notify_job_paused(job, reason_after, run_id)

        try:
            async with self._db_session_factory() as db:
                await self._repo.complete_run(
                    db=db,
                    run_id=run_id,
                    status=status,
                    result_summary=result_summary,
                    error_message=error_message,
                    conversation_id=conversation_id,
                    delivered=delivered,
                    condition_evaluation=condition_evaluation,
                    notice_due_at=notice_due_at,
                    parked_task_id=parked_task_id,
                    parked_payload=parked_payload,
                )
                await db.commit()
        except Exception:
            # The schedule is already advanced, so this cannot loop. But a run left
            # 'running' is no longer harmless: the healer would call it interrupted and
            # re-execute a job whose result was delivered. Close it with the columns the
            # table has always had, which is what the write that shaped this code lacked.
            logger.exception("Job %d: failed to record run %d; the job itself advanced", job.id, run_id)
            try:
                async with self._db_session_factory() as db:
                    await self._repo.close_run_minimally(db, run_id, status, error_message)
                    await db.commit()
            except Exception:
                logger.exception("Job %d: could not close run %d at all; the healer will sweep it", job.id, run_id)

        logger.info(
            "Job %d run %d finished: status=%s delivered=%s",
            job.id,
            run_id,
            status.value,
            delivered,
        )

        # Send WebSocket notification to user if they have active connections
        if self._socket_notification_manager:
            notification_payload = {
                "job_id": job.id,
                "job_name": job.name,
                "run_id": run_id,
                "status": status.value,
                "result_summary": result_summary,
                "error_message": error_message,
                # The badge cannot be derived from the status alone: a run keeps
                # 'auth_required' after it is answered, so "still waiting" is the task
                # id. Without it here a parked run-now renders in the past tense
                # ("Stopped for authorization") until the polled table corrects it.
                "parked_task_id": parked_task_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            sent = await self._socket_notification_manager.send_notification(
                job.user_id,
                notification_payload,
            )

            if sent:
                logger.info(f"Sent WebSocket notification for job {job.id} to user {job.user_id}")
            else:
                logger.debug(f"User {job.user_id} has no active WebSocket connections for job {job.id} notification")
