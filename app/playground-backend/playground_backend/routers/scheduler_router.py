"""Scheduler API router — exposes schedule management as MCP tools.

All endpoints are tagged "MCP" so FastApiMCP auto-exposes them as MCP tools,
allowing the orchestrator to create and manage scheduled jobs conversationally.
"""

import asyncio
import json
import logging
import os
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status

from ..config import config
from ..db.session import DbSession
from ..dependencies import require_auth, require_auth_or_bearer_token
from ..models.scheduled_job import (
    GenerateWatchParamsRequest,
    GenerateWatchParamsResponse,
    RunNowResponse,
    ScheduledJob,
    ScheduledJobCreate,
    ScheduledJobRun,
    ScheduledJobUpdate,
)
from ..models.user import User
from ..services.scheduler_engine import SchedulerEngine
from ..services.scheduler_service import _UNSET, SchedulerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scheduler")


def _get_scheduler_service(request: Request) -> SchedulerService:
    return request.app.state.scheduler_service  # type: ignore[no-any-return]


@router.post(
    "/generate-watch-params",
    response_model=GenerateWatchParamsResponse,
    summary="AI-generate check_args and condition_expr for a watch job.",
    description=(
        "Given an MCP tool spec and a natural-language query, uses an LLM to suggest "
        "`check_args` (JSON arguments for the tool) and `condition_expr` (JSONPath expression)."
    ),
)
async def generate_watch_params(
    data: GenerateWatchParamsRequest,
    _: User = Depends(require_auth),
) -> GenerateWatchParamsResponse:
    """Call Bedrock to auto-generate watch parameters for a given tool list and query."""
    tools_summary = json.dumps(
        [
            {"name": t.get("name"), "description": t.get("description"), "input_schema": t.get("input_schema")}
            for t in data.tools
        ],
        indent=2,
    )
    prompt = (
        "You are a scheduling-assistant. Given the list of available MCP tools below and the "
        "user's natural-language condition, generate:\n"
        "1. `check_tool`: the **name** of the single best-matching tool from the list.\n"
        "2. `check_args`: a minimal JSON object with the required arguments to call that tool.\n"
        "3. `condition_expr`: a JSONPath expression to extract a value from the tool's JSON response.\n"
        '4. `expected_value`: the expected value to compare against the extracted result. If the user wants to check "is not null", set this to null. Otherwise provide the exact string value expected.\n'
        "5. `message`: a concise notification text that will be sent to the user when "
        "the condition is met. Provide context about what was achieved (e.g., 'Pull request #123 has been merged').\n\n"
        f"Available tools:\n{tools_summary}\n\n"
        f"User condition: {data.query}\n\n"
        "Respond ONLY with a JSON object, no markdown fences, e.g.:\n"
        '{"check_tool": "tool_name", "check_args": {"param": "value"}, '
        '"condition_expr": "$.result.status", "expected_value": "success", "notification_message": "Task completed successfully"}'
    )

    def _invoke_bedrock() -> dict:  # runs in a thread
        import boto3

        client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "eu-central-1"),
        )
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        response = client.invoke_model(
            modelId=config.scheduler.ai_model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        result_body = json.loads(response["body"].read())
        text: str = result_body["content"][0]["text"]
        # Strip optional markdown fences
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {}

    try:
        result = await asyncio.to_thread(_invoke_bedrock)
    except Exception as exc:
        logger.warning("Bedrock watch-param generation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI generation service unavailable",
        ) from exc

    return GenerateWatchParamsResponse(
        check_tool=result.get("check_tool"),
        check_args=result.get("check_args"),
        condition_expr=result.get("condition_expr"),
        expected_value=result.get("expected_value"),
        notification_message=result.get("notification_message"),
    )


@router.post(
    "/jobs",
    response_model=ScheduledJob,
    status_code=status.HTTP_201_CREATED,
    summary="Create a scheduled job with push notifications to slack, email or google chat.",
    description=(
        "Create a new scheduled job that will run on behalf of the current user. "
        "For `job_type='task'`, supply a `sub_agent_id` referencing an `automated` sub-agent. "
        "For `job_type='watch'`, supply `check_tool`, `check_args`, `condition_expr`, and `expected_value` "
        "(JSONPath + expected value for comparison) so the scheduler can poll a condition before optionally invoking an agent. "
        "Supply a `delivery_channel_id` referencing a registered delivery channel."
    ),
    operation_id="scheduler_create_job",
    tags=["MCP"],
)
async def create_job(
    request: Request,
    data: ScheduledJobCreate,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    """Create a new scheduled job for the authenticated user."""
    service = _get_scheduler_service(request)
    try:
        return await service.create_job(db=db, data=data, actor=current_user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.get(
    "/jobs",
    response_model=list[ScheduledJob],
    summary="List scheduled jobs.",
    description="Returns all scheduled jobs owned by the current user.",
    tags=["MCP"],
    operation_id="scheduler_list_jobs",
)
async def list_jobs(
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> list[ScheduledJob]:
    service = _get_scheduler_service(request)
    return await service.list_jobs(db=db, user_id=current_user.id)


@router.get(
    "/jobs/{job_id}",
    response_model=ScheduledJob,
    summary="Get a scheduled job.",
    tags=["MCP"],
    operation_id="scheduler_get_job",
)
async def get_job(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.patch(
    "/jobs/{job_id}",
    response_model=ScheduledJob,
    summary="Update a scheduled job.",
    description="Partial update — only supplied fields are changed.",
    tags=["MCP"],
    operation_id="scheduler_update_job",
)
async def update_job(
    job_id: int,
    data: ScheduledJobUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)

    # Use model_fields_set to detect which fields were explicitly provided
    # If field is in model_fields_set, pass its value (including None to clear)
    # If field is not in model_fields_set, pass _UNSET to keep current value
    job = await service.update_job(
        db=db,
        job_id=job_id,
        data=data,
        actor=current_user,
        name=data.name if "name" in data.model_fields_set else _UNSET,
        prompt=data.prompt if "prompt" in data.model_fields_set else _UNSET,
        notification_message=data.notification_message if "notification_message" in data.model_fields_set else _UNSET,
        check_tool=data.check_tool if "check_tool" in data.model_fields_set else _UNSET,
        condition_expr=data.condition_expr if "condition_expr" in data.model_fields_set else _UNSET,
        expected_value=data.expected_value if "expected_value" in data.model_fields_set else _UNSET,
        llm_condition=data.llm_condition if "llm_condition" in data.model_fields_set else _UNSET,
        check_args=data.check_args if "check_args" in data.model_fields_set else _UNSET,
        delivery_channel_id=data.delivery_channel_id if "delivery_channel_id" in data.model_fields_set else _UNSET,
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.delete(
    "/jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a scheduled job.",
    # tags=["MCP"],
    operation_id="scheduler_delete_job",
)
async def delete_job(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> None:
    service = _get_scheduler_service(request)
    ok = await service.delete_job(db=db, job_id=job_id, actor=current_user)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")


@router.post(
    "/jobs/{job_id}/run-now",
    response_model=RunNowResponse,
    status_code=202,
    summary="Trigger an immediate test run for a scheduled job.",
    description=(
        "Dispatches the job asynchronously through the full execution pipeline: resolves the "
        "user's offline token, calls agent-runner (A2A), evaluates the watch condition if "
        "applicable, delivers the configured webhook notification, and records the run. "
        "Returns 202 immediately; the result is delivered via the scheduler_notification "
        "WebSocket event when execution completes."
    ),
)
async def run_job_now(
    job_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> RunNowResponse:
    """Immediately dispatch a job in the background and return 202."""
    service = _get_scheduler_service(request)
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    engine: SchedulerEngine = request.app.state.scheduler_engine
    run_id: int = await engine._repo.create_run(db, job_id)
    await db.commit()
    background_tasks.add_task(engine.run_job_now, job, run_id)
    return RunNowResponse(job_id=job_id, run_id=run_id)


@router.post(
    "/jobs/{job_id}/pause",
    response_model=ScheduledJob,
    summary="Pause a scheduled job.",
    tags=["MCP"],
    operation_id="scheduler_pause_job",
)
async def pause_job(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    ok = await service.pause_job(db=db, job_id=job_id, actor=current_user)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    assert job is not None
    return job


@router.post(
    "/jobs/{job_id}/resume",
    response_model=ScheduledJob,
    summary="Resume a paused scheduled job.",
    description="Re-enables the job and resets the failure counter.",
)
async def resume_job(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    ok = await service.resume_job(db=db, job_id=job_id, actor=current_user)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    assert job is not None
    return job


@router.get(
    "/jobs/{job_id}/runs",
    response_model=list[ScheduledJobRun],
    summary="List execution history for a scheduled job.",
    description="Returns the most recent execution runs (up to 50) for the given job.",
)
async def list_runs(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> list[ScheduledJobRun]:
    service = _get_scheduler_service(request)
    runs = await service.list_runs(db=db, job_id=job_id, user_id=current_user.id)
    if runs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return runs
