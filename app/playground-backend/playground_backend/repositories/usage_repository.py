"""Repository for usage tracking."""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class UsageRepository:
    """Repository for managing usage logs and billing unit details."""

    async def create_usage_log(
        self,
        db: AsyncSession,
        user_id: str,
        provider: str | None,
        model_name: str | None,
        total_cost_usd: Decimal,
        billing_unit_breakdown: dict[str, int],
        invoked_at: datetime,
        conversation_id: str | None = None,
        sub_agent_id: int | None = None,
        sub_agent_config_version_id: int | None = None,
        langsmith_run_id: str | None = None,
        langsmith_trace_id: str | None = None,
    ) -> int:
        """
        Create a usage log with billing unit details.

        Args:
            db: Database session
            user_id: User ID
            provider: Provider name
            model_name: Model name
            total_cost_usd: Total cost in USD
            billing_unit_breakdown: Dict of billing_unit -> count (only non-zero values)
            invoked_at: When the service was invoked
            conversation_id: Optional conversation ID
            sub_agent_id: Optional sub-agent ID
            sub_agent_config_version_id: Optional sub-agent config version ID
            langsmith_run_id: Optional LangSmith run ID
            langsmith_trace_id: Optional LangSmith trace ID

        Returns:
            ID of created usage log
        """
        # Insert usage log
        log_query = text("""
            INSERT INTO usage_logs (
                user_id, conversation_id, sub_agent_id, sub_agent_config_version_id,
                provider, model_name, total_cost_usd,
                langsmith_run_id, langsmith_trace_id, invoked_at
            )
            VALUES (
                :user_id, :conversation_id, :sub_agent_id, :sub_agent_config_version_id,
                :provider, :model_name, :total_cost_usd,
                :langsmith_run_id, :langsmith_trace_id, :invoked_at
            )
            RETURNING id
        """)

        result = await db.execute(
            log_query,
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "sub_agent_id": sub_agent_id,
                "sub_agent_config_version_id": sub_agent_config_version_id,
                "provider": provider,
                "model_name": model_name,
                "total_cost_usd": total_cost_usd,
                "langsmith_run_id": langsmith_run_id,
                "langsmith_trace_id": langsmith_trace_id,
                "invoked_at": invoked_at,
            },
        )
        usage_log_id = result.scalar_one()

        # Insert billing unit details (only non-zero values)
        if billing_unit_breakdown:
            await self._insert_billing_units(db, usage_log_id, billing_unit_breakdown)

        logger.info(
            f"Created usage log {usage_log_id} for user {user_id}: "
            f"{provider}/{model_name}, cost=${total_cost_usd}, "
            f"units={sum(billing_unit_breakdown.values())}"
        )

        return usage_log_id

    async def _insert_billing_units(
        self,
        db: AsyncSession,
        usage_log_id: int,
        billing_unit_breakdown: dict[str, int],
    ) -> None:
        """
        Insert billing unit details for a usage log.

        Args:
            db: Database session
            usage_log_id: Usage log ID
            billing_unit_breakdown: Dict of billing_unit -> count (only positive values)
        """
        # Prepare batch insert with parameterized values (prevents SQL injection)
        values_to_insert = [
            {"usage_log_id": usage_log_id, "billing_unit": billing_unit, "unit_count": count}
            for billing_unit, count in billing_unit_breakdown.items()
            if count > 0  # Only store non-zero values
        ]

        if not values_to_insert:
            return

        query = text("""
            INSERT INTO usage_billing_units (usage_log_id, billing_unit, unit_count)
            VALUES (:usage_log_id, :billing_unit, :unit_count)
        """)

        # Execute as batch with executemany for efficiency
        await db.execute(query, values_to_insert)

    async def batch_create_usage_logs(
        self,
        db: AsyncSession,
        logs: list[dict[str, Any]],
    ) -> list[int]:
        """
        Batch create multiple usage logs with billing unit details.

        Args:
            db: Database session
            logs: List of dicts with usage log data

        Returns:
            List of created usage log IDs
        """
        usage_log_ids = []

        for log_data in logs:
            billing_unit_breakdown = log_data.pop("billing_unit_breakdown", {})

            usage_log_id = await self.create_usage_log(
                db=db,
                billing_unit_breakdown=billing_unit_breakdown,
                **log_data,
            )
            usage_log_ids.append(usage_log_id)

        logger.info(f"Batch created {len(usage_log_ids)} usage logs")

        return usage_log_ids

    async def get_usage_summary(
        self,
        db: AsyncSession,
        user_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get usage summary for a user or time period.

        Args:
            db: Database session
            user_id: Optional user ID filter
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            Dict with total_cost_usd, total_requests, etc.
        """
        where_conditions = []
        params: dict[str, Any] = {}

        if user_id:
            where_conditions.append("user_id = :user_id")
            params["user_id"] = user_id

        if start_date:
            where_conditions.append("invoked_at >= :start_date")
            params["start_date"] = start_date

        if end_date:
            where_conditions.append("invoked_at < :end_date")
            params["end_date"] = end_date

        where_clause = ""
        if where_conditions:
            where_clause = "WHERE " + " AND ".join(where_conditions)

        query = text(f"""
            SELECT
                COALESCE(SUM(total_cost_usd), 0) as total_cost_usd,
                COUNT(*) as total_requests,
                MIN(invoked_at) as period_start,
                MAX(invoked_at) as period_end
            FROM usage_logs
            {where_clause}
        """)

        result = await db.execute(query, params)
        row = result.mappings().first()

        return dict(row) if row else {}

    async def get_usage_by_sub_agent(
        self,
        db: AsyncSession,
        user_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get usage breakdown by sub-agent for a user.

        Args:
            db: Database session
            user_id: User ID
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            List of dicts with sub_agent_id, total_cost_usd, etc.
        """
        where_conditions = ["u.user_id = :user_id"]
        params: dict[str, Any] = {"user_id": user_id}

        if start_date:
            where_conditions.append("u.invoked_at >= :start_date")
            params["start_date"] = start_date

        if end_date:
            where_conditions.append("u.invoked_at < :end_date")
            params["end_date"] = end_date

        query = text(f"""
            WITH billing_unit_summary AS (
                SELECT 
                    td.usage_log_id,
                    SUM(CASE 
                        WHEN rce.flow_direction = 'input' 
                          OR (rce.flow_direction IS NULL AND td.billing_unit LIKE '%input%' AND td.billing_unit NOT LIKE '%cache_read%')
                        THEN td.unit_count 
                        ELSE 0 
                    END) as input_tokens,
                    SUM(CASE 
                        WHEN rce.flow_direction = 'output' 
                          OR (rce.flow_direction IS NULL AND (td.billing_unit LIKE '%output%' OR td.billing_unit LIKE '%cache_read%' OR td.billing_unit LIKE '%reasoning%'))
                        THEN td.unit_count 
                        ELSE 0 
                    END) as output_tokens
                FROM usage_billing_units td
                JOIN usage_logs u ON u.id = td.usage_log_id
                LEFT JOIN rate_cards rc ON rc.provider = u.provider 
                  AND (
                    (rc.model_name_pattern IS NULL AND rc.model_name = u.model_name)
                    OR
                    (rc.model_name_pattern IS NOT NULL AND u.model_name ~ rc.model_name_pattern)
                  )
                LEFT JOIN rate_card_entries rce ON rce.rate_card_id = rc.id 
                  AND rce.billing_unit = td.billing_unit
                  AND rce.effective_from <= u.invoked_at
                  AND (rce.effective_until IS NULL OR rce.effective_until > u.invoked_at)
                WHERE {" AND ".join(where_conditions)}
                GROUP BY td.usage_log_id
            )
            SELECT
                u.sub_agent_id,
                s.name as sub_agent_name,
                SUM(u.total_cost_usd) as total_cost_usd,
                COUNT(*) as total_requests,
                COALESCE(SUM(ts.input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(ts.output_tokens), 0) as total_output_tokens
            FROM usage_logs u
            LEFT JOIN billing_unit_summary ts ON ts.usage_log_id = u.id
            LEFT JOIN sub_agents s ON u.sub_agent_id = s.id
            WHERE {" AND ".join(where_conditions)} AND u.sub_agent_id IS NOT NULL
            GROUP BY u.sub_agent_id, s.name
            ORDER BY total_cost_usd DESC
        """)

        result = await db.execute(query, params)
        return [dict(row) for row in result.mappings()]

    async def get_usage_by_conversation(
        self,
        db: AsyncSession,
        user_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Get usage breakdown by conversation for a user.

        Args:
            db: Database session
            user_id: User ID
            start_date: Optional start date
            end_date: Optional end date
            limit: Max conversations to return

        Returns:
            List of dicts with conversation_id, total_cost_usd, etc.
        """
        where_conditions = ["user_id = :user_id", "conversation_id IS NOT NULL"]
        params: dict[str, Any] = {"user_id": user_id, "limit": limit}

        if start_date:
            where_conditions.append("invoked_at >= :start_date")
            params["start_date"] = start_date

        if end_date:
            where_conditions.append("invoked_at < :end_date")
            params["end_date"] = end_date

        query = text(f"""
            SELECT
                conversation_id,
                SUM(total_cost_usd) as total_cost_usd,
                COUNT(*) as total_requests,
                MIN(invoked_at) as first_message_at,
                MAX(invoked_at) as last_message_at
            FROM usage_logs
            WHERE {" AND ".join(where_conditions)}
            GROUP BY conversation_id
            ORDER BY last_message_at DESC
            LIMIT :limit
        """)

        result = await db.execute(query, params)
        return [dict(row) for row in result.mappings()]

    async def get_billing_unit_breakdown(
        self,
        db: AsyncSession,
        user_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get usage breakdown by billing unit type for a user.

        Args:
            db: Database session
            user_id: User ID
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            List of dicts with billing_unit, total_count, percentage
        """
        where_conditions = ["u.user_id = :user_id"]
        params: dict[str, Any] = {"user_id": user_id}

        if start_date:
            where_conditions.append("u.invoked_at >= :start_date")
            params["start_date"] = start_date

        if end_date:
            where_conditions.append("u.invoked_at < :end_date")
            params["end_date"] = end_date

        query = text(f"""
            WITH billing_unit_totals AS (
                SELECT
                    t.billing_unit,
                    SUM(t.unit_count) as total_count
                FROM usage_billing_units t
                JOIN usage_logs u ON t.usage_log_id = u.id
                WHERE {" AND ".join(where_conditions)}
                GROUP BY t.billing_unit
            ),
            grand_total AS (
                SELECT SUM(total_count) as total FROM billing_unit_totals
            )
            SELECT
                billing_unit,
                total_count,
                ROUND((total_count::numeric / NULLIF(grand_total.total, 0) * 100), 2) as percentage
            FROM billing_unit_totals, grand_total
            ORDER BY total_count DESC
        """)

        result = await db.execute(query, params)
        return [dict(row) for row in result.mappings()]

    async def list_usage_logs(
        self,
        db: AsyncSession,
        user_id: str | None = None,
        conversation_id: str | None = None,
        sub_agent_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        List usage logs with optional filters and pagination.

        Args:
            db: Database session
            user_id: Optional user ID filter
            conversation_id: Optional conversation ID filter
            sub_agent_id: Optional sub-agent ID filter
            start_date: Optional start date
            end_date: Optional end date
            page: Page number (1-indexed)
            limit: Items per page

        Returns:
            Tuple of (logs with token details, total_count)
        """
        where_conditions = []
        params: dict[str, Any] = {"limit": limit, "offset": (page - 1) * limit}

        if user_id:
            where_conditions.append("user_id = :user_id")
            params["user_id"] = user_id

        if conversation_id:
            where_conditions.append("conversation_id = :conversation_id")
            params["conversation_id"] = conversation_id

        if sub_agent_id:
            where_conditions.append("sub_agent_id = :sub_agent_id")
            params["sub_agent_id"] = sub_agent_id

        if start_date:
            where_conditions.append("invoked_at >= :start_date")
            params["start_date"] = start_date

        if end_date:
            where_conditions.append("invoked_at < :end_date")
            params["end_date"] = end_date

        where_clause = ""
        if where_conditions:
            where_clause = "WHERE " + " AND ".join(where_conditions)

        # Get total count
        count_query = text(f"""
            SELECT COUNT(*) as total
            FROM usage_logs
            {where_clause}
        """)
        count_result = await db.execute(count_query, params)
        total = count_result.scalar()

        # Get logs with billing unit details
        query = text(f"""
            SELECT
                u.*,
                sa.name as sub_agent_name,
                COALESCE(
                    json_agg(
                        json_build_object('billing_unit', t.billing_unit, 'unit_count', t.unit_count)
                        ORDER BY t.billing_unit
                    ) FILTER (WHERE t.id IS NOT NULL),
                    '[]'::json
                ) as billing_unit_details
            FROM usage_logs u
            LEFT JOIN usage_billing_units t ON u.id = t.usage_log_id
            LEFT JOIN sub_agents sa ON u.sub_agent_id = sa.id AND sa.deleted_at IS NULL
            {where_clause}
            GROUP BY u.id, sa.name
            ORDER BY u.invoked_at DESC
            LIMIT :limit OFFSET :offset
        """)

        result = await db.execute(query, params)
        logs = [dict(row) for row in result.mappings()]

        return logs, total
