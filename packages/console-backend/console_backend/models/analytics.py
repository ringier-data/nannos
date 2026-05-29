"""Pydantic models for platform analytics KPI responses."""

from decimal import Decimal

from pydantic import BaseModel, Field

# Shared time-series data point models


class TimeSeriesPoint(BaseModel):
    """A single data point in a time series."""

    period: str = Field(..., description="ISO date string for the period start (e.g., '2026-03-02')")
    value: int | Decimal


class TimeSeriesSummary(BaseModel):
    """Summary statistics for a time-series KPI."""

    current: int | Decimal = Field(..., description="Current period value")
    previous: int | Decimal = Field(..., description="Previous period value")
    change_percent: float | None = Field(None, description="Percentage change from previous to current")


# Active Users (DAU / WAU)


class ActiveUsersResponse(BaseModel):
    """Response for active users over time (DAU or WAU)."""

    data: list[TimeSeriesPoint] = Field(default_factory=list)
    summary: TimeSeriesSummary
    granularity: str = Field(..., description="Aggregation granularity: 'day' or 'week'")


# Churn Rate


class ChurnSummary(BaseModel):
    """Summary of churn metrics."""

    previous_period_active_users: int
    current_period_active_users: int
    churned_users: int
    new_or_reactivated_users: int
    churn_rate_percent: float | None = None


class ChurnRateResponse(BaseModel):
    """Response for churn rate analysis."""

    data: list[TimeSeriesPoint] = Field(default_factory=list, description="Churn rate percentage over rolling windows")
    summary: ChurnSummary


# User Engagement


class EngagementBucket(BaseModel):
    """A single engagement frequency bucket."""

    bucket: str = Field(..., description="Bucket label (e.g., '1', '2-4', '5-10', '>10')")
    user_count: int
    percent_of_users: float


class EngagementResponse(BaseModel):
    """Response for user engagement distribution."""

    data: list[EngagementBucket] = Field(default_factory=list)
    total_active_users: int


# User Cohorts / Lifetime


class CohortBucket(BaseModel):
    """A single user cohort bucket."""

    cohort: str = Field(..., description="Cohort label (e.g., 'New (1 week)', 'Veteran (3+ months)')")
    user_count: int
    percent_of_users: float


class CohortResponse(BaseModel):
    """Response for user lifetime cohort analysis."""

    data: list[CohortBucket] = Field(default_factory=list)
    total_active_users: int


# Cost over Time


class CostTimeSeriesPoint(BaseModel):
    """A cost data point in a time series."""

    period: str = Field(..., description="ISO date string for the period start")
    total_cost_usd: Decimal
    request_count: int


class CostSummary(BaseModel):
    """Summary of cost metrics."""

    total_cost_usd: Decimal
    total_requests: int
    average_cost_per_request: Decimal | None = None
    previous_period_cost_usd: Decimal | None = None
    change_percent: float | None = None


class CostOverTimeResponse(BaseModel):
    """Response for global cost over time."""

    data: list[CostTimeSeriesPoint] = Field(default_factory=list)
    summary: CostSummary
    granularity: str = Field(..., description="Aggregation granularity: 'day', 'week', or 'month'")
