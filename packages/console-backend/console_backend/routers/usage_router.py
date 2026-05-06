"""API router for usage tracking and reporting."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..authorization import check_capability
from ..db.session import DbSession
from ..dependencies import require_auth, require_auth_or_bearer_token
from ..models.usage import (
    BillingUnitDetail,
    DetailedUsageReport,
    UsageLog,
    UsageLogBatchCreate,
    UsageLogCreate,
    UsageLogsList,
    UsageSummary,
)
from ..models.user import User
from ..services.usage_service import UsageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


def get_usage_service(request: Request) -> UsageService:
    """Get usage service from app state."""
    return request.app.state.usage_service


@router.post("/log", status_code=status.HTTP_201_CREATED)
async def log_usage(
    request: Request,
    log_data: UsageLogCreate,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
):
    """
    Log a single usage entry.

    Requires Bearer token authentication. Token's sub claim must match the user_id in the log record.
    Used by orchestrator and sub-agents for cost tracking.
    """
    usage_service = get_usage_service(request)
    try:
        usage_log_id = await usage_service.log_usage(
            db=db,
            user_id=current_user.id,
            provider=log_data.provider,
            model_name=log_data.model_name,
            billing_unit_breakdown=log_data.billing_unit_breakdown,
            invoked_at=log_data.invoked_at,
            conversation_id=log_data.conversation_id,
            sub_agent_id=log_data.sub_agent_id,
            sub_agent_config_version_id=log_data.sub_agent_config_version_id,
            langsmith_run_id=log_data.langsmith_run_id,
            langsmith_trace_id=log_data.langsmith_trace_id,
        )
        await db.commit()
        return {"id": usage_log_id, "status": "logged"}
    except Exception as e:
        logger.error(f"Failed to log usage: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to log usage: {str(e)}",
        )


@router.post("/batch-log", status_code=status.HTTP_201_CREATED)
async def batch_log_usage(
    request: Request,
    batch_data: UsageLogBatchCreate,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
):
    """
    Batch log multiple usage entries.

    Requires Bearer token authentication. Token's sub claim must match all user_ids in the batch.
    Used by orchestrator and sub-agents for efficient cost tracking.
    """
    usage_service = get_usage_service(request)

    try:
        logs = []
        for log_obj in batch_data.logs:
            log_dict = log_obj.model_dump()
            log_dict["user_id"] = current_user.id
            logs.append(log_dict)

        usage_log_ids = await usage_service.batch_log_usage(db=db, logs=logs)
        await db.commit()

        return {
            "count": len(usage_log_ids),
            "ids": usage_log_ids,
            "status": "logged",
        }
    except Exception as e:
        logger.error(f"Failed to batch log usage: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to batch log usage: {str(e)}",
        )


@router.get("/my-summary", response_model=UsageSummary)
async def get_my_usage_summary(
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth),
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
):
    """
    Get usage summary for the current user.

    Returns total cost, request count, and breakdowns for the specified period.
    """
    usage_service = get_usage_service(request)
    start_date = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        summary = await usage_service.get_user_summary(
            db=db,
            user_id=current_user.id,
            start_date=start_date,
        )
        return summary
    except Exception as e:
        logger.error(f"Failed to get usage summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get usage summary: {str(e)}",
        )


@router.get("/my-detailed", response_model=DetailedUsageReport)
async def get_my_detailed_usage(
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth),
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
):
    """
    Get detailed usage report for the current user.

    Includes breakdown by sub-agent, conversation, and billing units.
    """
    usage_service = get_usage_service(request)
    start_date = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        report = await usage_service.get_detailed_report(
            db=db,
            user_id=current_user.id,
            start_date=start_date,
        )
        return report
    except Exception as e:
        logger.error(f"Failed to get detailed usage: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get detailed usage: {str(e)}",
        )


@router.get("/my-logs", response_model=UsageLogsList)
async def get_my_usage_logs(
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    days: int = Query(30, ge=1, le=365),
    conversation_id: str | None = Query(None),
    sub_agent_id: int | None = Query(None),
):
    """
    List usage logs for the current user with pagination.

    Can filter by conversation or sub-agent.
    """
    usage_service = get_usage_service(request)
    start_date = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        logs, total = await usage_service.repository.list_usage_logs(
            db=db,
            user_id=current_user.id,
            conversation_id=conversation_id,
            sub_agent_id=sub_agent_id,
            start_date=start_date,
            page=page,
            limit=limit,
        )

        log_models = []
        for log in logs:
            billing_unit_details = [
                BillingUnitDetail(billing_unit=bd["billing_unit"], unit_count=bd["unit_count"])
                for bd in log.get("billing_unit_details", [])
            ]

            log_models.append(
                UsageLog(
                    id=log["id"],
                    user_id=log["user_id"],
                    conversation_id=log.get("conversation_id"),
                    sub_agent_id=log.get("sub_agent_id"),
                    sub_agent_config_version_id=log.get("sub_agent_config_version_id"),
                    sub_agent_name=log.get("sub_agent_name"),
                    scheduled_job_id=log.get("scheduled_job_id"),
                    scheduled_job_name=log.get("scheduled_job_name"),
                    catalog_id=str(log["catalog_id"]) if log.get("catalog_id") else None,
                    catalog_name=log.get("catalog_name"),
                    provider=log["provider"],
                    model_name=log["model_name"],
                    total_cost_usd=log["total_cost_usd"],
                    langsmith_run_id=log.get("langsmith_run_id"),
                    langsmith_trace_id=log.get("langsmith_trace_id"),
                    invoked_at=log["invoked_at"],
                    logged_at=log["logged_at"],
                    billing_unit_details=billing_unit_details,
                )
            )

        return UsageLogsList(
            logs=log_models,
            total=total,
            page=page,
            limit=limit,
        )

    except Exception as e:
        logger.error(f"Failed to get usage logs: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get usage logs: {str(e)}",
        )


# Admin endpoints


@router.get("/admin/global-summary", response_model=UsageSummary)
async def get_global_usage_summary(
    db: DbSession,
    current_user: User = Depends(require_auth),
    days: int = Query(30, ge=1, le=365),
    request: Request = None,  # type: ignore[assignment]
):
    """
    Get global usage summary (all users).

    Requires admin role.
    """
    if not check_capability(current_user.role, "users", "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    usage_service = get_usage_service(request)
    start_date = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        summary = await usage_service.get_global_summary(
            db=db,
            start_date=start_date,
        )
        return summary

    except Exception as e:
        logger.error(f"Failed to get global usage summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get global usage summary: {str(e)}",
        )


@router.get("/admin/user/{user_id}/summary", response_model=UsageSummary)
async def get_user_usage_summary(
    user_id: str,
    db: DbSession,
    current_user: User = Depends(require_auth),
    days: int = Query(30, ge=1, le=365),
    request: Request = None,  # type: ignore[assignment]
):
    """
    Get usage summary for a specific user.

    Requires admin role.
    """
    if not check_capability(current_user.role, "users", "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    usage_service = get_usage_service(request)
    start_date = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        summary = await usage_service.get_user_summary(
            db=db,
            user_id=user_id,
            start_date=start_date,
        )
        return summary

    except Exception as e:
        logger.error(f"Failed to get user usage summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user usage summary: {str(e)}",
        )
