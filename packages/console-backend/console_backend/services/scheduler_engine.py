"""Scheduler engine — tick loop that claims due jobs and dispatches them to agent-runner.

The engine runs inside the agent-console backend process as a background asyncio task.
It owns:
  - Job claiming (FOR UPDATE SKIP LOCKED)
  - User token resolution (KMS → Keycloak refresh)
  - Dispatching to agent-runner via the native a2a-sdk v1.1.0 streaming client
  - Recording outcomes in scheduled_job_runs
  - Advancing or disabling jobs based on results
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text

from ..repositories.model_defaults_repository import ModelDefaultsRepository
from .llm_gateway import gateway_chat
from .watch_evaluator import WatchEvaluator, WatchOutcome
from ..models.scheduled_job import JobRunStatus, JobType, ScheduledJob
from ..repositories.delivery_channel_repository import DeliveryChannelRepository
from ..repositories.scheduled_job_repository import ScheduledJobRepository, compute_next_run
from ..services.scheduler_token_service import SchedulerTokenService
from ..services.socket_notification_manager import SocketNotificationManager
from ..utils.a2a_dispatch import dispatch_streaming

logger = logging.getLogger(__name__)

# How long a run may sit in 'running' before the healer calls it interrupted.
#
# Generous on purpose. The healer now sweeps on every tick, so this bound is the only
# thing standing between a legitimately slow dispatch and a run record that says it
# failed while the agent is still working. Runs this process started are excluded
# outright (_in_flight), so the window only has to cover dispatches this process cannot
# see: another instance's, or a crashed predecessor's.
STUCK_RUN_THRESHOLD = "30 minutes"


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
    ) -> None:
        self._repo = repo
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
        """Fail runs left in 'running' longer than STUCK_RUN_THRESHOLD.

        A run gets stranded when the process is killed mid-dispatch, or when recording
        its outcome fails. Nothing else ever revisits the row: it reads as work in
        progress forever, with no duration and no error.

        This used to run only at startup, which meant a stranded run cleared on the next
        restart or never. It now runs on every tick, so a strand self-clears within the
        threshold wherever it came from. Runs this process is still dispatching are
        excluded — see _in_flight.
        """
        try:
            async with self._db_session_factory() as db:
                result = await db.execute(
                    text(f"""
                        UPDATE scheduled_job_runs
                        SET
                            status       = 'failed',
                            completed_at = NOW(),
                            error_message = 'Run was interrupted before completing (process restart or unhandled error)'
                        WHERE status = 'running'
                          AND started_at < NOW() - INTERVAL '{STUCK_RUN_THRESHOLD}'
                          AND NOT (id = ANY(:in_flight))
                        RETURNING id
                    """),
                    {"in_flight": list(self._in_flight)},
                )
                healed = [r["id"] for r in result.mappings().all()]
                await db.commit()
            if healed:
                logger.warning("Healed %d stuck 'running' run(s): %s", len(healed), healed)
        except Exception:
            logger.exception("Failed to heal stuck runs")

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
        run-record creation.

        If run_id is provided (pre-created by the caller) the engine will skip
        creating a new run record and use the supplied ID instead.
        """
        logger.info("Manual run-now triggered for job %d by user request", job.id)
        await self._dispatch_job(job, run_id=run_id)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Unhandled error in scheduler tick")
            await asyncio.sleep(self._tick_interval)

    async def _tick(self) -> None:
        await self._heal_stuck_runs()

        async with self._db_session_factory() as db:
            jobs = await self._repo.claim_due_jobs(db, limit=self._claim_limit)
            await db.commit()

        if not jobs:
            return

        logger.info("Scheduler claiming %d due job(s)", len(jobs))
        tasks = [asyncio.create_task(self._dispatch_job(job)) for job in jobs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for job, result in zip(jobs, results):
            if isinstance(result, Exception):
                logger.error("Job %d dispatch raised an unhandled exception: %s", job.id, result)

    async def _dispatch_job(self, job: ScheduledJob, run_id: int | None = None) -> None:
        """Resolve user token, build A2A payload, call agent-runner, record result."""
        if run_id is None:
            async with self._db_session_factory() as db:
                run_id = await self._repo.create_run(db, job.id)
                await db.commit()

        logger.info("Dispatching job %d (run %d) to agent-runner", job.id, run_id)

        self._in_flight.add(run_id)
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
                    )
                    return

                # Watch jobs: decide here whether anything is happening. A poll that
                # does not trigger dispatches nothing at all — and knowing the outcome
                # before dispatch is what lets the trigger choose its target (an agent,
                # or a phone call).
                watch_outcome: WatchOutcome | None = None
                if self._watch_evaluator.can_evaluate(job):
                    watch_outcome = await self._watch_evaluator.evaluate(db, job, access_token)

                    if watch_outcome.error:
                        await self._finalize(
                            run_id=run_id,
                            job=job,
                            status=JobRunStatus.FAILED,
                            error_message=watch_outcome.error,
                            delivered=False,
                            last_check_result=watch_outcome.check_result,
                            condition_evaluation=watch_outcome.evaluation,
                        )
                        return

                    if not watch_outcome.condition_met:
                        await self._finalize(
                            run_id=run_id,
                            job=job,
                            status=JobRunStatus.CONDITION_NOT_MET,
                            delivered=False,
                            last_check_result=watch_outcome.check_result,
                            condition_evaluation=watch_outcome.evaluation,
                        )
                        return

                # Build the A2A message args for agent-runner
                parts, metadata, push_config = await self._build_message_args(
                    job, run_id, access_token, db, watch_outcome=watch_outcome
                )

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
            status, result_summary, error_msg, conversation_id = self._parse_result(result_data)

            # Push notification is delivered by the A2A SDK (BasePushNotificationSender)
            # inside agent-runner when pushNotificationConfig is included in the payload.
            await self._finalize(
                run_id=run_id,
                job=job,
                status=status,
                result_summary=result_summary,
                error_message=error_msg,
                conversation_id=conversation_id,
                delivered=(job.delivery_channel_id is not None),
                # From the local evaluation: the scheduler performed the check, so the
                # runner has no reason to echo it back.
                last_check_result=(
                    watch_outcome.check_result if watch_outcome else result_data.get("last_check_result")
                ),
                condition_evaluation=watch_outcome.evaluation if watch_outcome else None,
            )

        except httpx.HTTPStatusError as e:
            logger.error("agent-runner HTTP error for job %d: %s", job.id, e)
            try:
                await self._finalize(
                    run_id=run_id,
                    job=job,
                    status=JobRunStatus.FAILED,
                    error_message=f"agent-runner HTTP {e.response.status_code}: {e.response.text[:500]}",
                    delivered=False,
                )
            except Exception:
                logger.exception("Failed to finalize run %s for job %d after HTTP error", run_id, job.id)
        except Exception as e:
            logger.exception("Unexpected error dispatching job %d", job.id)
            try:
                await self._finalize(
                    run_id=run_id,
                    job=job,
                    status=JobRunStatus.FAILED,
                    error_message=str(e),
                    delivered=False,
                )
            except Exception:
                logger.exception("Failed to finalize run %s for job %d after dispatch error", run_id, job.id)
        finally:
            self._in_flight.discard(run_id)

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
        metadata: dict[str, Any] = {
            "scheduled_job_id": job.id,
            "scheduled_job_run_id": run_id,
            "job_type": job.job_type.value,
            # The job's IANA timezone, so the runner's tool-less LLM calls
            # (condition eval, notification generation) can be told "now".
            "timezone": job.timezone or None,
        }

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
        if job.voice_call:
            voice_agent_id = await self._resolve_voice_agent_id(db)
            if voice_agent_id is not None:
                metadata["sub_agent_id"] = voice_agent_id
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

        # Fetch delivery channel and attach push notification config so the A2A SDK
        # registers it for the task and BasePushNotificationSender can deliver it
        # upon completion.  The channel secret is sent as X-A2A-Notification-Token
        # so the webhook receiver can verify ownership of the notification.
        #
        # This is why a triggered watch is dispatched even when it only sends a
        # notification and runs no agent: delivery is the push sender's job, and the
        # payload is an A2A Task envelope that the delivery channels normalise. Posting
        # it from here would duplicate that contract across three receivers.
        push_config: dict[str, str] | None = None
        if job.delivery_channel_id is not None:
            channel = await self._delivery_channel_repo.get_channel_for_dispatch(db, job.delivery_channel_id)
            if channel:
                push_config = {"url": channel["webhook_url"], "token": channel["secret"]}

        return parts, metadata, push_config

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

        prompt = (
            "Write the notification a user receives when a scheduled watch triggers. "
            "One or two sentences, factual, highlighting what changed. Reply with the "
            "message text only, no preamble.\n\n"
            f"Watch: {job.name}\n"
            f"Result:\n{json.dumps(check_result, indent=2, default=str)[:6000]}"
        )
        try:
            message = await gateway_chat(prompt, model=model, max_tokens=256)
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

    def _parse_result(self, data: dict[str, Any]) -> tuple[JobRunStatus, str | None, str | None, str | None]:
        """Extract structured result fields from agent-runner A2A response.

        Supports two response formats:
        1. A2A Task format: result is a Task object with artifacts containing JSON metadata
        2. Legacy custom format: result.metadata contains the scheduler fields directly

        Returns:
            Tuple of (status, result_summary, error_message, conversation_id)
        """
        # JSON-RPC error response (e.g. validation failure) — no "result" key
        if "error" in data and "result" not in data:
            error_msg = data["error"].get("message", "JSON-RPC error")
            return (
                JobRunStatus.FAILED,
                None,
                f"A2A request error: {error_msg}",
                None,
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

        return (
            status,
            meta.get("agent_message"),
            meta.get("error_message"),
            conversation_id,
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
        condition_evaluation: dict | None = None,
    ) -> None:
        """Persist run outcome and advance job state."""
        success = status in (JobRunStatus.SUCCESS, JobRunStatus.CONDITION_NOT_MET)

        try:
            next_run_at = compute_next_run(
                schedule_kind=job.schedule_kind,
                cron_expr=job.cron_expr,
                interval_seconds=job.interval_seconds,
                run_at=job.run_at,
                after=datetime.now(timezone.utc),
                tz=job.timezone,
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
                # Disable the job via direct SQL (system action, no user actor)
                await db.execute(
                    text("""
                        UPDATE scheduled_jobs
                        SET enabled = FALSE,
                            paused_reason = 'Watch condition met (one-time trigger)',
                            updated_at = :now
                        WHERE id = :job_id
                    """),
                    {"job_id": job.id, "now": datetime.now(timezone.utc)},
                )

            await self._repo.complete_job(
                db=db,
                job_id=job.id,
                success=success,
                next_run_at=next_run_at,
                last_check_result=last_check_result,
                paused_reason=paused_reason,
            )
            await db.commit()

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
                )
                await db.commit()
        except Exception:
            # The schedule is already advanced, so this cannot loop. The run is left for
            # the healer, and the job keeps working while somebody reads this.
            logger.exception("Job %d: failed to record run %d; the job itself advanced", job.id, run_id)

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
