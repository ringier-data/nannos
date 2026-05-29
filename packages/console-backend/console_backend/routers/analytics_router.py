"""API router for platform analytics KPIs (admin-only)."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..db.session import DbSession
from ..dependencies import require_admin
from ..models.analytics import (
    ActiveUsersResponse,
    ChurnRateResponse,
    CohortResponse,
    CostOverTimeResponse,
    EngagementResponse,
)
from ..models.user import User
from ..services.analytics_service import AnalyticsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


def get_analytics_service(request: Request) -> AnalyticsService:
    """Get analytics service from app state."""
    return request.app.state.analytics_service


@router.get("/active-users", response_model=ActiveUsersResponse)
async def get_active_users(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
    granularity: Literal["day", "week"] = Query("day", description="Aggregation granularity"),
):
    """
    Get active users over time (DAU or WAU).

    Returns time-series data points and a summary with current vs. previous period comparison.
    Requires admin role.
    """
    service = get_analytics_service(request)

    try:
        return await service.get_active_users(db=db, days=days, granularity=granularity)
    except Exception as e:
        logger.error(f"Failed to get active users: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve active users data",
        )


@router.get("/churn", response_model=ChurnRateResponse)
async def get_churn_rate(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
    days: int = Query(30, ge=7, le=365, description="Number of days to look back"),
):
    """
    Get churn rate analysis with rolling weekly windows.

    Returns time-series of churn rates and a current snapshot summary.
    Requires admin role.
    """
    service = get_analytics_service(request)

    try:
        return await service.get_churn_rate(db=db, days=days)
    except Exception as e:
        logger.error(f"Failed to get churn rate: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve churn rate data",
        )


@router.get("/engagement", response_model=EngagementResponse)
async def get_engagement(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
    days: int = Query(7, ge=1, le=365, description="Number of days to look back"),
):
    """
    Get user engagement distribution by conversation frequency.

    Returns engagement buckets (1, 2-4, 5-10, >10 conversations).
    Requires admin role.
    """
    service = get_analytics_service(request)

    try:
        return await service.get_engagement(db=db, days=days)
    except Exception as e:
        logger.error(f"Failed to get engagement data: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve engagement data",
        )


@router.get("/cohorts", response_model=CohortResponse)
async def get_cohorts(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
    days: int = Query(90, ge=7, le=365, description="Number of days to look back"),
):
    """
    Get user lifetime cohort distribution.

    Returns cohort buckets (New, Young, Established, Veteran).
    Requires admin role.
    """
    service = get_analytics_service(request)

    try:
        return await service.get_cohorts(db=db, days=days)
    except Exception as e:
        logger.error(f"Failed to get cohort data: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve cohort data",
        )


@router.get("/cost-over-time", response_model=CostOverTimeResponse)
async def get_cost_over_time(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
    granularity: Literal["day", "week", "month"] = Query("day", description="Aggregation granularity"),
):
    """
    Get global platform cost over time.

    Returns time-series cost data and a summary with period comparison.
    Requires admin role.
    """
    service = get_analytics_service(request)

    try:
        return await service.get_cost_over_time(db=db, days=days, granularity=granularity)
    except Exception as e:
        logger.error(f"Failed to get cost data: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve cost data",
        )
