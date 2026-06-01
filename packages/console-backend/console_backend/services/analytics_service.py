"""Service for platform analytics KPI queries."""

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.analytics import (
    ActiveUsersResponse,
    ChurnRateResponse,
    ChurnSummary,
    CohortBucket,
    CohortResponse,
    CostOverTimeResponse,
    CostSummary,
    CostTimeSeriesPoint,
    EngagementBucket,
    EngagementResponse,
    TimeSeriesPoint,
    TimeSeriesSummary,
)

logger = logging.getLogger(__name__)

Granularity = Literal["day", "week", "month"]

# Default cache TTL: 5 minutes. Analytics data changes slowly.
_CACHE_TTL_SECONDS = 300


class _AnalyticsCache:
    """Simple in-memory TTL cache for analytics results."""

    def __init__(self, ttl: int = _CACHE_TTL_SECONDS):
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._store.clear()


class AnalyticsService:
    """Read-only service for platform analytics KPIs."""

    def __init__(self, cache_ttl: int = _CACHE_TTL_SECONDS):
        self._cache = _AnalyticsCache(ttl=cache_ttl)

    async def get_active_users(
        self,
        db: AsyncSession,
        days: int = 30,
        granularity: Granularity = "day",
    ) -> ActiveUsersResponse:
        """Get active users over time (DAU or WAU depending on granularity)."""
        cache_key = f"active_users:{days}:{granularity}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        start_date = datetime.now(timezone.utc) - timedelta(days=days)

        query = text("""
            SELECT
                DATE_TRUNC(:granularity, invoked_at AT TIME ZONE 'UTC')::date AS period,
                COUNT(DISTINCT user_id) AS active_users
            FROM usage_logs
            WHERE invoked_at >= :start_date
            GROUP BY period
            ORDER BY period ASC
        """)

        result = await db.execute(query, {"granularity": granularity, "start_date": start_date})
        rows = result.fetchall()

        data = [TimeSeriesPoint(period=str(row.period), value=row.active_users) for row in rows]

        # Compute summary: current vs previous period
        current, previous = await self._compute_active_users_summary(db, days)

        change_percent = None
        if previous > 0:
            change_percent = round(100.0 * (current - previous) / previous, 1)

        response = ActiveUsersResponse(
            data=data,
            summary=TimeSeriesSummary(current=current, previous=previous, change_percent=change_percent),
            granularity=granularity,
        )
        self._cache.set(cache_key, response)
        return response

    async def _compute_active_users_summary(self, db: AsyncSession, days: int) -> tuple[int, int]:
        """Compute current and previous period active user counts."""
        now = datetime.now(timezone.utc)
        current_start = now - timedelta(days=days)
        previous_start = now - timedelta(days=days * 2)

        query = text("""
            SELECT
                COUNT(DISTINCT CASE WHEN invoked_at >= :current_start THEN user_id END) AS current_count,
                COUNT(DISTINCT CASE WHEN invoked_at >= :previous_start AND invoked_at < :current_start THEN user_id END) AS previous_count
            FROM usage_logs
            WHERE invoked_at >= :previous_start
        """)

        result = await db.execute(query, {"current_start": current_start, "previous_start": previous_start})
        row = result.fetchone()
        return (row.current_count or 0, row.previous_count or 0)

    async def get_churn_rate(
        self,
        db: AsyncSession,
        days: int = 30,
    ) -> ChurnRateResponse:
        """Get churn rate with rolling week-over-week windows within the time range."""
        cache_key = f"churn_rate:{days}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        start_date = datetime.now(timezone.utc) - timedelta(days=days)

        # Rolling churn data points (weekly windows)
        query = text("""
            WITH week_boundaries AS (
                SELECT generate_series(
                    DATE_TRUNC('week', CAST(:start_date AS timestamptz)),
                    DATE_TRUNC('week', now()),
                    INTERVAL '1 week'
                )::date AS week_start
            ),
            weekly_users AS (
                SELECT
                    wb.week_start,
                    array_agg(DISTINCT ul.user_id) AS users
                FROM week_boundaries wb
                LEFT JOIN usage_logs ul
                    ON ul.invoked_at >= wb.week_start
                    AND ul.invoked_at < wb.week_start + INTERVAL '7 days'
                GROUP BY wb.week_start
            ),
            churn_calc AS (
                SELECT
                    wu.week_start,
                    COALESCE(array_length(wu.users, 1), 0) AS current_users,
                    COALESCE(array_length(prev.users, 1), 0) AS previous_users,
                    (
                        SELECT COUNT(*)
                        FROM unnest(prev.users) AS pu(uid)
                        WHERE pu.uid != ALL(COALESCE(wu.users, ARRAY[]::text[]))
                    ) AS churned
                FROM weekly_users wu
                LEFT JOIN weekly_users prev ON prev.week_start = wu.week_start - INTERVAL '7 days'
                WHERE prev.week_start IS NOT NULL
            )
            SELECT
                week_start AS period,
                CASE WHEN previous_users > 0
                    THEN ROUND(100.0 * churned / previous_users, 2)
                    ELSE 0
                END AS churn_rate
            FROM churn_calc
            ORDER BY week_start ASC
        """)

        result = await db.execute(query, {"start_date": start_date})
        rows = result.fetchall()

        data = [TimeSeriesPoint(period=str(row.period), value=Decimal(str(row.churn_rate))) for row in rows]

        # Current snapshot summary
        summary = await self._compute_churn_summary(db)

        response = ChurnRateResponse(data=data, summary=summary)
        self._cache.set(cache_key, response)
        return response

    async def _compute_churn_summary(self, db: AsyncSession) -> ChurnSummary:
        """Compute current churn snapshot (last 7 days vs prior 7 days)."""
        query = text("""
            WITH current_week_users AS (
                SELECT DISTINCT user_id
                FROM usage_logs
                WHERE invoked_at >= now() - INTERVAL '7 days'
            ),
            previous_week_users AS (
                SELECT DISTINCT user_id
                FROM usage_logs
                WHERE invoked_at >= now() - INTERVAL '14 days'
                  AND invoked_at < now() - INTERVAL '7 days'
            ),
            churned AS (
                SELECT COUNT(*) AS churned_count
                FROM previous_week_users p
                WHERE p.user_id NOT IN (SELECT user_id FROM current_week_users)
            )
            SELECT
                (SELECT COUNT(*) FROM previous_week_users) AS previous_week_active_users,
                (SELECT COUNT(*) FROM current_week_users) AS current_week_active_users,
                (SELECT churned_count FROM churned) AS churned_users_count,
                (SELECT COUNT(*) FROM current_week_users) - (SELECT churned_count FROM churned) AS new_or_reactivated,
                ROUND(
                    100.0 * (SELECT churned_count FROM churned) /
                    NULLIF((SELECT COUNT(*) FROM previous_week_users), 0),
                    2
                ) AS churn_rate_percent
        """)

        result = await db.execute(query)
        row = result.fetchone()

        return ChurnSummary(
            previous_period_active_users=row.previous_week_active_users or 0,
            current_period_active_users=row.current_week_active_users or 0,
            churned_users=row.churned_users_count or 0,
            new_or_reactivated_users=row.new_or_reactivated or 0,
            churn_rate_percent=float(row.churn_rate_percent) if row.churn_rate_percent else None,
        )

    async def get_engagement(
        self,
        db: AsyncSession,
        days: int = 7,
    ) -> EngagementResponse:
        """Get user engagement distribution by conversation frequency."""
        cache_key = f"engagement:{days}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        start_date = datetime.now(timezone.utc) - timedelta(days=days)

        query = text("""
            WITH user_conversations AS (
                SELECT
                    user_id,
                    COUNT(DISTINCT conversation_id) AS conversation_count
                FROM usage_logs
                WHERE invoked_at >= :start_date
                  AND conversation_id IS NOT NULL
                GROUP BY user_id
            ),
            engagement_buckets AS (
                SELECT
                    CASE
                        WHEN conversation_count > 10 THEN '>10'
                        WHEN conversation_count >= 5 THEN '5-10'
                        WHEN conversation_count >= 2 THEN '2-4'
                        ELSE '1'
                    END AS bucket,
                    COUNT(*) AS user_count
                FROM user_conversations
                GROUP BY bucket
            )
            SELECT
                bucket,
                user_count,
                ROUND(100.0 * user_count / NULLIF(SUM(user_count) OVER (), 0), 2) AS percent_of_users
            FROM engagement_buckets
            ORDER BY CASE bucket
                WHEN '>10' THEN 1
                WHEN '5-10' THEN 2
                WHEN '2-4' THEN 3
                WHEN '1' THEN 4
            END
        """)

        result = await db.execute(query, {"start_date": start_date})
        rows = result.fetchall()

        data = [
            EngagementBucket(
                bucket=row.bucket,
                user_count=row.user_count,
                percent_of_users=float(row.percent_of_users or 0),
            )
            for row in rows
        ]

        total = sum(b.user_count for b in data)

        response = EngagementResponse(data=data, total_active_users=total)
        self._cache.set(cache_key, response)
        return response

    async def get_cohorts(
        self,
        db: AsyncSession,
        days: int = 90,
    ) -> CohortResponse:
        """Get user lifetime cohort distribution."""
        cache_key = f"cohorts:{days}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        start_date = datetime.now(timezone.utc) - timedelta(days=days)

        query = text("""
            WITH user_activity_span AS (
                SELECT
                    user_id,
                    COUNT(DISTINCT DATE_TRUNC('week', invoked_at)::date) AS weeks_active
                FROM usage_logs
                WHERE invoked_at >= :start_date
                GROUP BY user_id
            ),
            cohort_buckets AS (
                SELECT
                    CASE
                        WHEN weeks_active = 1 THEN 'New (1 week)'
                        WHEN weeks_active <= 4 THEN 'Young (2-4 weeks)'
                        WHEN weeks_active <= 13 THEN 'Established (1-3 months)'
                        ELSE 'Veteran (3+ months)'
                    END AS cohort,
                    COUNT(*) AS user_count
                FROM user_activity_span
                GROUP BY cohort
            )
            SELECT
                cohort,
                user_count,
                ROUND(100.0 * user_count / NULLIF(SUM(user_count) OVER (), 0), 2) AS percent_of_users
            FROM cohort_buckets
            ORDER BY CASE cohort
                WHEN 'New (1 week)' THEN 1
                WHEN 'Young (2-4 weeks)' THEN 2
                WHEN 'Established (1-3 months)' THEN 3
                WHEN 'Veteran (3+ months)' THEN 4
            END
        """)

        result = await db.execute(query, {"start_date": start_date})
        rows = result.fetchall()

        data = [
            CohortBucket(
                cohort=row.cohort,
                user_count=row.user_count,
                percent_of_users=float(row.percent_of_users or 0),
            )
            for row in rows
        ]

        total = sum(b.user_count for b in data)

        response = CohortResponse(data=data, total_active_users=total)
        self._cache.set(cache_key, response)
        return response

    async def get_cost_over_time(
        self,
        db: AsyncSession,
        days: int = 30,
        granularity: Granularity = "day",
    ) -> CostOverTimeResponse:
        """Get global cost over time."""
        cache_key = f"cost_over_time:{days}:{granularity}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        now = datetime.now(timezone.utc)
        start_date = now - timedelta(days=days)
        previous_start = now - timedelta(days=days * 2)

        # Time series data
        query = text("""
            SELECT
                DATE_TRUNC(:granularity, invoked_at AT TIME ZONE 'UTC')::date AS period,
                COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd,
                COUNT(*) AS request_count
            FROM usage_logs
            WHERE invoked_at >= :start_date
            GROUP BY period
            ORDER BY period ASC
        """)

        result = await db.execute(query, {"granularity": granularity, "start_date": start_date})
        rows = result.fetchall()

        data = [
            CostTimeSeriesPoint(
                period=str(row.period),
                total_cost_usd=row.total_cost_usd,
                request_count=row.request_count,
            )
            for row in rows
        ]

        # Summary with current vs previous period
        summary_query = text("""
            SELECT
                COALESCE(SUM(CASE WHEN invoked_at >= :current_start THEN total_cost_usd ELSE 0 END), 0) AS current_cost,
                COUNT(CASE WHEN invoked_at >= :current_start THEN 1 END) AS current_requests,
                COALESCE(SUM(CASE WHEN invoked_at < :current_start THEN total_cost_usd ELSE 0 END), 0) AS previous_cost
            FROM usage_logs
            WHERE invoked_at >= :previous_start
        """)

        result = await db.execute(summary_query, {"current_start": start_date, "previous_start": previous_start})
        row = result.fetchone()

        current_cost = row.current_cost or Decimal("0")
        previous_cost = row.previous_cost or Decimal("0")
        current_requests = row.current_requests or 0

        change_percent = None
        if previous_cost > 0:
            change_percent = round(100.0 * float(current_cost - previous_cost) / float(previous_cost), 1)

        avg_cost = Decimal("0")
        if current_requests > 0:
            avg_cost = current_cost / current_requests

        response = CostOverTimeResponse(
            data=data,
            summary=CostSummary(
                total_cost_usd=current_cost,
                total_requests=current_requests,
                average_cost_per_request=avg_cost,
                previous_period_cost_usd=previous_cost,
                change_percent=change_percent,
            ),
            granularity=granularity,
        )
        self._cache.set(cache_key, response)
        return response
