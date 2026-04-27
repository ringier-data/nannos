"""Scheduler engine — tick loop that claims due jobs and dispatches them to agent-runner.

The engine runs inside the agent-console backend process as a background asyncio task.
It owns:
  - Job claiming (FOR UPDATE SKIP LOCKED)
  - User token resolution (KMS → Keycloak refresh)
  - Dispatching to agent-runner via A2A message/send
  - Recording outcomes in scheduled_job_runs
  - Advancing or disabling jobs based on results
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text

from ..models.scheduled_job import JobRunStatus, JobType, ScheduledJob
from ..repositories.delivery_channel_repository import DeliveryChannelRepository
from ..repositories.scheduled_job_repository import ScheduledJobRepository, compute_next_run
from ..services.scheduler_token_service import SchedulerTokenService
from ..services.socket_notification_manager import SocketNotificationManager

logger = logging.getLogger(__name__)


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
        """Mark runs that have been stuck in 'running' state for >10 minutes as failed.

        Runs can get stuck if the process was killed mid-dispatch or if _finalize
        raised an unhandled exception.  This cleanup runs once at startup so the
        UI does not show stale 'Running' entries indefinitely.
        """
        try:
            async with self._db_session_factory() as db:
                result = await db.execute(
                    text("""
                        UPDATE scheduled_job_runs
                        SET
                            status       = 'failed',
                            completed_at = NOW(),
                            error_message = 'Run was interrupted before completing (process restart or unhandled error)'
                        WHERE status = 'running'
                          AND started_at < NOW() - INTERVAL '10 minutes'
                        RETURNING id
                    """)
                )
                healed = result.rowcount
                await db.commit()
            if healed:
                logger.warning("Healed %d stuck 'running' run(s) on startup", healed)
        except Exception:
            logger.exception("Failed to heal stuck runs on startup")

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

                # Build A2A message payload for agent-runner
                payload = await self._build_a2a_payload(job, run_id, access_token, db)

            # Dispatch to agent-runner via SSE streaming to avoid CloudFront 60s
            # idle timeout: the first SSE byte ("working" status) arrives within ms,
            # resetting the timeout clock for each subsequent event.
            result_data = await self._send_streaming_request(payload, access_token)

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
                last_check_result=result_data.get("last_check_result"),
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

    async def _build_a2a_payload(self, job: ScheduledJob, run_id: int, access_token: str, db: Any) -> dict[str, Any]:
        """Build the JSON-RPC A2A message/send payload for agent-runner."""
        metadata: dict[str, Any] = {
            "scheduled_job_id": job.id,
            "scheduled_job_run_id": run_id,
            "job_type": job.job_type.value,
        }

        if job.sub_agent_id is not None:
            # sub-agent config will be fetched by agent-runner using the sub_agent_id
            metadata["sub_agent_id"] = job.sub_agent_id

        if job.job_type.value == "watch":
            metadata["watch"] = {
                "check_tool": job.check_tool,
                "check_args": job.check_args or {},
                "condition_expr": job.condition_expr,
                "expected_value": job.expected_value,
                "llm_condition": job.llm_condition,
                "last_check_result": job.last_check_result,
            }

        # Select the appropriate message content based on job type
        if job.job_type.value == "task":
            message_text = job.prompt or "Execute the task you are designed for."
        else:
            message_text = job.notification_message or ""

        # Voice-call dispatch: override target to voice-agent and build
        # DataPart (sub_agent_id config) + TextPart (prompt) message.
        if job.voice_call and job.job_type.value == "task":
            voice_agent_id = await self._resolve_voice_agent_id(db)
            if voice_agent_id is not None:
                metadata["sub_agent_id"] = voice_agent_id
            else:
                logger.warning("voice_call=True for job %d but voice-agent not found in DB", job.id)

            parts: list[dict[str, Any]] = [
                {
                    "kind": "data",
                    "data": {"sub_agent_id": job.sub_agent_id},
                    "metadata": {"mimeType": "application/json"},
                },
            ]
            if message_text and message_text != "Execute the task you are designed for.":
                parts.append({"kind": "text", "text": message_text})
        else:
            parts = [{"kind": "text", "text": message_text}]

        params: dict[str, Any] = {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": parts,
                "metadata": metadata,
            }
        }

        # Fetch delivery channel and attach push notification config so the A2A SDK
        # registers it for the task and BasePushNotificationSender can deliver it
        # upon completion.  The channel secret is sent as X-A2A-Notification-Token
        # so the webhook receiver can verify ownership of the notification.
        if job.delivery_channel_id is not None:
            channel = await self._delivery_channel_repo.get_channel_for_dispatch(db, job.delivery_channel_id)
            if channel:
                push_config: dict[str, Any] = {
                    "url": channel["webhook_url"],
                    "token": channel["secret"],
                }
                params["configuration"] = {"pushNotificationConfig": push_config}

        return {
            "jsonrpc": "2.0",
            "method": "message/stream",
            "id": f"scheduler-job-{job.id}",
            "params": params,
        }

    async def _resolve_voice_agent_id(self, db: Any) -> int | None:
        """Look up the voice-agent sub_agent_id from the DB (system-owned)."""
        result = await db.execute(
            text(
                "SELECT id FROM sub_agents WHERE name = 'voice-agent' AND owner_user_id = 'system' AND deleted_at IS NULL LIMIT 1"
            )
        )
        row = result.scalar_one_or_none()
        return row

    async def _send_streaming_request(self, payload: dict[str, Any], access_token: str) -> dict[str, Any]:
        """POST to agent-runner using message/stream (SSE), consume events, return a
        synthetic pseudo-task dict that _parse_result already understands.

        SSE keeps bytes flowing so CloudFront/ALB idle-timeout never fires, even
        for long-running jobs.  The first 'working' status-update event arrives
        within milliseconds of the request being accepted.
        """
        last_artifact_text: str | None = None
        context_id: str | None = None
        final_state: str = "completed"

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=5.0)) as client:
            async with client.stream(
                "POST",
                f"{self._agent_runner_url}/",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "text/event-stream",
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:") :].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Skipping non-JSON SSE line: %s", raw[:120])
                        continue

                    # JSON-RPC error (e.g. "Streaming is not supported by the
                    # agent") — surface as a real exception so the caller marks
                    # the run as FAILED rather than silently succeeding.
                    if "error" in event and "result" not in event:
                        err = event["error"]
                        raise RuntimeError(
                            f"A2A JSON-RPC error from agent-runner "
                            f"(code={err.get('code')}): {err.get('message', 'unknown error')}"
                        )

                    result = event.get("result", {})
                    kind = result.get("kind")

                    if kind == "artifact-update":
                        context_id = result.get("contextId") or context_id
                        artifact = result.get("artifact", {})
                        for part in artifact.get("parts", []):
                            if isinstance(part, dict) and part.get("kind") == "text":
                                last_artifact_text = part.get("text", "")
                                break

                    elif kind == "status-update":
                        context_id = result.get("contextId") or context_id
                        state = result.get("status", {}).get("state", "working")
                        if state in ("completed", "failed"):
                            final_state = state

        # Build a synthetic response in the same shape _parse_result already
        # handles (A2A Task format with artifacts list).
        task_obj: dict[str, Any] = {
            "kind": "task",
            "contextId": context_id,
            "status": {"state": final_state},
            "artifacts": [],
        }
        if last_artifact_text is not None:
            task_obj["artifacts"] = [{"parts": [{"kind": "text", "text": last_artifact_text}]}]
        return {"result": task_obj}

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
    ) -> None:
        """Persist run outcome and advance job state."""
        success = status in (JobRunStatus.SUCCESS, JobRunStatus.CONDITION_NOT_MET)

        next_run_at = compute_next_run(
            schedule_kind=job.schedule_kind,
            cron_expr=job.cron_expr,
            interval_seconds=job.interval_seconds,
            run_at=job.run_at,
            after=datetime.now(timezone.utc),
        )

        async with self._db_session_factory() as db:
            await self._repo.complete_run(
                db=db,
                run_id=run_id,
                status=status,
                result_summary=result_summary,
                error_message=error_message,
                conversation_id=conversation_id,
                delivered=delivered,
            )

            # Disable watch job if destroy_after_trigger is True and condition was successfully met
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
