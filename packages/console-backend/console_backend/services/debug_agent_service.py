"""Service for dispatching debug agent requests via agent-runner."""

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.bug_report import BugReportResponse, BugReportStatus
from ..models.user import User
from ..services.bug_report_service import BugReportService

logger = logging.getLogger(__name__)

# Timeout for debug agent execution (5 minutes)
DEBUG_AGENT_TIMEOUT_SECONDS = 300


class DebugAgentService:
    """Thin dispatcher that triggers a debug agent via agent-runner.

    The debug agent is a sub-agent registered with system_role = 'debug'. When triggered,
    this service sets the bug report status to 'investigating' and dispatches
    an A2A message to agent-runner which handles all execution (local/remote).
    The debug agent uses MCP tools autonomously to update the bug report.
    """

    def __init__(
        self,
        bug_report_service: BugReportService,
        db_session_factory: Any,
        agent_runner_url: str,
        oauth_service: Any,  # OidcOAuth2Client
    ) -> None:
        self._bug_report_service = bug_report_service
        self._db_session_factory = db_session_factory
        self._agent_runner_url = agent_runner_url.rstrip("/")
        self._oauth_service = oauth_service

    async def get_debug_agent_id(self, db: AsyncSession) -> str | None:
        """Find the active debug agent (system_role='debug', approved with default_version)."""
        result = await db.execute(
            text(
                "SELECT id FROM sub_agents "
                "WHERE system_role = 'debug' AND default_version IS NOT NULL AND deleted_at IS NULL "
                "ORDER BY created_at ASC LIMIT 1"
            )
        )
        row = result.scalar_one_or_none()
        return str(row) if row is not None else None

    async def trigger_debug(
        self,
        db: AsyncSession,
        actor: User,
        bug_report: BugReportResponse,
        user_access_token: str,
    ) -> None:
        """Trigger debug agent execution for a bug report.

        Sets status to 'investigating' immediately, then dispatches async to agent-runner.
        The user's access token is exchanged for an agent-runner-scoped token so the
        debug agent acts on behalf of the triggering admin.
        """
        # Find the active debug agent
        debug_agent_id = await self.get_debug_agent_id(db)
        if debug_agent_id is None:
            raise ValueError("No active debug agent configured. Register a sub-agent with type='debug'.")

        # Generate a conversation_id for the debug run (used as LangSmith trace thread)
        debug_conversation_id = str(uuid.uuid4())

        # Set status to investigating and store the debug_conversation_id immediately
        await self._bug_report_service.update_status(
            db=db,
            actor=actor,
            report_id=bug_report.id,
            new_status=BugReportStatus.INVESTIGATING,
        )
        await db.execute(
            text(
                "UPDATE bug_reports SET debug_conversation_id = :debug_conversation_id, updated_at = NOW() WHERE id = :id"
            ),
            {"debug_conversation_id": debug_conversation_id, "id": bug_report.id},
        )
        await db.commit()

        # Dispatch async — fire and forget
        asyncio.create_task(
            self._run_debug(
                report_id=bug_report.id,
                sub_agent_id=debug_agent_id,
                bug_report=bug_report,
                user_access_token=user_access_token,
                context_id=debug_conversation_id,
            )
        )

    async def _run_debug(
        self,
        report_id: str,
        sub_agent_id: str,
        bug_report: BugReportResponse,
        user_access_token: str,
        context_id: str,
    ) -> None:
        """Execute debug agent via agent-runner (async background task)."""
        try:
            # Exchange user token for agent-runner audience (acts on behalf of user)
            access_token = await self._oauth_service.exchange_token(
                subject_token=user_access_token,
                target_client_id="agent-runner",
            )
            payload = self._build_a2a_payload(report_id, sub_agent_id, bug_report, context_id)
            await self._send_streaming_request(payload, access_token)
            logger.info(f"Debug agent completed for bug report {report_id}")
        except Exception:
            logger.exception(f"Debug agent failed for bug report {report_id}")
        finally:
            # If the agent finished (success or failure) but didn't update the
            # status itself via MCP tools, revert from "investigating" so the
            # report doesn't get stuck.
            try:
                async with self._db_session_factory() as db:
                    await db.execute(
                        text(
                            "UPDATE bug_reports SET status = :status, updated_at = NOW() "
                            "WHERE id = :id AND status = 'investigating'"
                        ),
                        {"status": BugReportStatus.ACKNOWLEDGED.value, "id": report_id},
                    )
                    await db.commit()
            except Exception:
                logger.exception(f"Failed to revert bug report {report_id} status after debug agent completion")

    def _build_a2a_payload(
        self,
        report_id: str,
        sub_agent_id: str,
        bug_report: BugReportResponse,
        context_id: str,
    ) -> dict[str, Any]:
        """Build JSON-RPC A2A message/stream payload for agent-runner."""
        # DataPart with structured context
        data_part: dict[str, Any] = {
            "kind": "data",
            "data": {
                "bug_report_id": report_id,
                "conversation_id": bug_report.conversation_id,
                "message_id": bug_report.message_id,
                "task_id": bug_report.task_id,
                "description": bug_report.description,
            },
            "metadata": {"mimeType": "application/json"},
        }

        # TextPart with natural language instructions
        text_part: dict[str, Any] = {
            "kind": "text",
            "text": (
                f"Investigate bug report {report_id}. "
                f"Analyze LangSmith traces for the given task and conversation, "
                f"check for duplicate GitHub issues, create one if needed, "
                f"then update the bug report status and external link using the provided tools."
            ),
        }

        return {
            "jsonrpc": "2.0",
            "method": "message/stream",
            "id": f"debug-{report_id}",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "contextId": context_id,
                    "role": "user",
                    "parts": [data_part, text_part],
                    "metadata": {
                        "sub_agent_id": sub_agent_id,
                    },
                },
            },
        }

    async def _send_streaming_request(self, payload: dict[str, Any], access_token: str) -> None:
        """POST to agent-runner using message/stream (SSE), consume until completion."""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=float(DEBUG_AGENT_TIMEOUT_SECONDS),
                write=30.0,
                pool=5.0,
            )
        ) as client:
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
                        continue

                    # Check for JSON-RPC error
                    if "error" in event and "result" not in event:
                        err = event["error"]
                        raise RuntimeError(
                            f"A2A JSON-RPC error from agent-runner "
                            f"(code={err.get('code')}): {err.get('message', 'unknown error')}"
                        )

                    result = event.get("result", {})
                    kind = result.get("kind")
                    if kind == "status-update":
                        state = result.get("status", {}).get("state", "working")
                        if state == "failed":
                            raise RuntimeError("Debug agent reported failure")
