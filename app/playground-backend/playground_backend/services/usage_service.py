"""Service for usage tracking and reporting."""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.usage import (
    BillingUnitBreakdown,
    DetailedUsageReport,
    UsageByConversation,
    UsageBySubAgent,
    UsageSummary,
)
from ..repositories.usage_repository import UsageRepository
from ..services.rate_card_service import RateCardService

logger = logging.getLogger(__name__)


class UsageService:
    """Service for tracking and reporting usage."""

    def __init__(
        self, usage_repository: UsageRepository | None = None, rate_card_service: RateCardService | None = None
    ):
        """Initialize usage service.

        Args:
            usage_repository: Optional usage repository instance.
            rate_card_service: Optional rate card service instance.
        """
        self._repository = usage_repository
        self._rate_card_service = rate_card_service

    def set_repository(self, usage_repository):
        """Set the usage repository (dependency injection)."""
        self._repository = usage_repository

    def set_rate_card_service(self, rate_card_service):
        """Set the rate card service (dependency injection)."""
        self._rate_card_service = rate_card_service

    @property
    def repository(self) -> UsageRepository:
        """Get the usage repository, raising error if not set."""
        if self._repository is None:
            raise RuntimeError("UsageRepository not injected. Call set_repository() during initialization.")
        return self._repository

    @property
    def rate_card_service(self) -> RateCardService:
        """Get the rate card service, raising error if not set."""
        if self._rate_card_service is None:
            raise RuntimeError("RateCardService not injected. Call set_rate_card_service() during initialization.")
        return self._rate_card_service

    async def log_usage(
        self,
        db: AsyncSession,
        user_id: str,
        provider: str | None,
        model_name: str | None,
        billing_unit_breakdown: dict[str, int],
        invoked_at: datetime,
        conversation_id: str | None = None,
        sub_agent_id: int | None = None,
        sub_agent_config_version_id: int | None = None,
        langsmith_run_id: str | None = None,
        langsmith_trace_id: str | None = None,
    ) -> int:
        """
        Log usage with automatic cost calculation.

        Args:
            db: Database session
            user_id: User ID
            provider: Provider name (optional for agent-specific rate cards)
            model_name: Model name (optional for agent-specific rate cards)
            billing_unit_breakdown: Dict of billing_unit -> count (only non-zero)
            invoked_at: When the service was invoked
            conversation_id: Optional conversation ID
            sub_agent_id: Optional sub-agent ID
            sub_agent_config_version_id: Optional sub-agent config version ID
            langsmith_run_id: Optional LangSmith run ID
            langsmith_trace_id: Optional LangSmith trace ID

        Returns:
            ID of created usage log

        Raises:
            ValueError: If cost calculation fails due to missing rate cards
        """
        # If sub_agent_id is provided but sub_agent_config_version_id is not, fetch the default version
        if sub_agent_id is not None and sub_agent_config_version_id is None:
            result = await db.execute(
                text("""
                    SELECT cv.id
                    FROM sub_agents sa
                    JOIN sub_agent_config_versions cv 
                        ON sa.id = cv.sub_agent_id AND sa.default_version = cv.version
                    WHERE sa.id = :sub_agent_id
                """),
                {"sub_agent_id": sub_agent_id},
            )
            row = result.first()
            if row and row[0] is not None:
                sub_agent_config_version_id = row[0]
                logger.debug(
                    f"Auto-populated sub_agent_config_version_id={sub_agent_config_version_id} for sub_agent_id={sub_agent_id}"
                )

        # Calculate cost using rate cards
        try:
            total_cost = await self.rate_card_service.calculate_cost(
                db=db,
                provider=provider,
                model_name=model_name,
                billing_unit_breakdown=billing_unit_breakdown,
                as_of=invoked_at,
                sub_agent_config_version_id=sub_agent_config_version_id,
            )
        except Exception as e:
            logger.error(f"Failed to calculate cost for {provider}/{model_name}: {e}")
            # Use zero cost if calculation fails, but log the error
            total_cost = Decimal("0.00")

        # Create usage log with billing unit details
        usage_log_id = await self.repository.create_usage_log(
            db=db,
            user_id=user_id,
            provider=provider,
            model_name=model_name,
            total_cost_usd=total_cost,
            billing_unit_breakdown=billing_unit_breakdown,
            invoked_at=invoked_at,
            conversation_id=conversation_id,
            sub_agent_id=sub_agent_id,
            sub_agent_config_version_id=sub_agent_config_version_id,
            langsmith_run_id=langsmith_run_id,
            langsmith_trace_id=langsmith_trace_id,
        )

        logger.info(
            f"Logged usage {usage_log_id} for user {user_id}: "
            f"{provider}/{model_name}, {sum(billing_unit_breakdown.values())} units, "
            f"cost=${total_cost:.6f}"
        )

        return usage_log_id

    async def batch_log_usage(
        self,
        db: AsyncSession,
        logs: list[dict],
    ) -> list[int]:
        """
        Batch log multiple usages with cost calculation.

        Args:
            db: Database session
            logs: List of usage log dicts (each with user_id, provider, model_name, billing_unit_breakdown, etc.)

        Returns:
            List of created usage log IDs
        """
        enriched_logs = []

        # Collect sub_agent_ids that need default version lookup
        sub_agent_ids_needing_version = set()
        for log_data in logs:
            sub_agent_id = log_data.get("sub_agent_id")
            sub_agent_config_version_id = log_data.get("sub_agent_config_version_id")
            if sub_agent_id is not None and sub_agent_config_version_id is None:
                sub_agent_ids_needing_version.add(sub_agent_id)

        # Batch fetch default versions for all needed sub_agents
        default_versions = {}
        if sub_agent_ids_needing_version:
            result = await db.execute(
                text("""
                    SELECT sa.id, cv.id
                    FROM sub_agents sa
                    JOIN sub_agent_config_versions cv 
                        ON sa.id = cv.sub_agent_id AND sa.default_version = cv.version
                    WHERE sa.id = ANY(:ids)
                """),
                {"ids": list(sub_agent_ids_needing_version)},
            )
            default_versions = {row[0]: row[1] for row in result}
            logger.debug(f"Fetched default versions for {len(default_versions)} sub-agents")

        for log_data in logs:
            # Auto-populate sub_agent_config_version_id if not provided
            sub_agent_id = log_data.get("sub_agent_id")
            sub_agent_config_version_id = log_data.get("sub_agent_config_version_id")
            if sub_agent_id is not None and sub_agent_config_version_id is None:
                sub_agent_config_version_id = default_versions.get(sub_agent_id)
                if sub_agent_config_version_id is not None:
                    log_data["sub_agent_config_version_id"] = sub_agent_config_version_id

            # Calculate cost for each log
            billing_unit_breakdown = log_data["billing_unit_breakdown"]
            try:
                total_cost = await self.rate_card_service.calculate_cost(
                    db=db,
                    provider=log_data["provider"],
                    model_name=log_data["model_name"],
                    billing_unit_breakdown=billing_unit_breakdown,
                    as_of=log_data.get("invoked_at", datetime.now(timezone.utc)),
                    sub_agent_config_version_id=log_data.get("sub_agent_config_version_id"),
                )
            except Exception as e:
                logger.error(f"Failed to calculate cost for {log_data['provider']}/{log_data['model_name']}: {e}")
                total_cost = Decimal("0.00")

            enriched_logs.append({**log_data, "total_cost_usd": total_cost})

        # Batch insert
        usage_log_ids = await self.repository.batch_create_usage_logs(db, enriched_logs)

        logger.info(f"Batch logged {len(usage_log_ids)} usage entries")

        return usage_log_ids

    async def get_user_summary(
        self,
        db: AsyncSession,
        user_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> UsageSummary:
        """
        Get usage summary for a user.

        Args:
            db: Database session
            user_id: User ID
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            UsageSummary with totals and breakdowns
        """
        summary_data = await self.repository.get_usage_summary(
            db=db,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        )

        return UsageSummary(
            total_cost_usd=summary_data.get("total_cost_usd", Decimal("0.00")),
            total_requests=summary_data.get("total_requests", 0),
            providers={},  # TODO: Add provider breakdown
            models={},  # TODO: Add model breakdown
            period_start=summary_data.get("period_start") or start_date or datetime.now(timezone.utc),
            period_end=summary_data.get("period_end") or end_date or datetime.now(timezone.utc),
        )

    async def get_detailed_report(
        self,
        db: AsyncSession,
        user_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> DetailedUsageReport:
        """
        Get detailed usage report with all breakdowns.

        Args:
            db: Database session
            user_id: User ID
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            DetailedUsageReport with summary, sub-agent, conversation, and token breakdowns
        """
        # Get summary
        summary = await self.get_user_summary(db, user_id, start_date, end_date)

        # Get sub-agent breakdown
        sub_agent_data = await self.repository.get_usage_by_sub_agent(
            db=db,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        )
        by_sub_agent = [
            UsageBySubAgent(
                sub_agent_id=row["sub_agent_id"],
                sub_agent_name=row["sub_agent_name"] or f"Sub-Agent {row['sub_agent_id']}",
                total_cost_usd=row["total_cost_usd"],
                total_requests=row["total_requests"],
                total_input_tokens=row["total_input_tokens"] or 0,
                total_output_tokens=row["total_output_tokens"] or 0,
            )
            for row in sub_agent_data
        ]

        # Get conversation breakdown
        conversation_data = await self.repository.get_usage_by_conversation(
            db=db,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        )
        by_conversation = [
            UsageByConversation(
                conversation_id=row["conversation_id"],
                total_cost_usd=row["total_cost_usd"],
                total_requests=row["total_requests"],
                first_message_at=row["first_message_at"],
                last_message_at=row["last_message_at"],
            )
            for row in conversation_data
        ]

        # Get billing unit breakdown
        billing_unit_data = await self.repository.get_billing_unit_breakdown(
            db=db,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        )
        billing_unit_breakdown = [
            BillingUnitBreakdown(
                billing_unit=row["billing_unit"],
                total_count=row["total_count"],
                percentage=float(row["percentage"] or 0),
            )
            for row in billing_unit_data
        ]

        return DetailedUsageReport(
            summary=summary,
            by_sub_agent=by_sub_agent,
            by_conversation=by_conversation,
            billing_unit_breakdown=billing_unit_breakdown,
        )

    async def get_global_summary(
        self,
        db: AsyncSession,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> UsageSummary:
        """
        Get global usage summary (all users).

        Args:
            db: Database session
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            UsageSummary with global totals
        """
        summary_data = await self.repository.get_usage_summary(
            db=db,
            user_id=None,  # No user filter = global
            start_date=start_date,
            end_date=end_date,
        )

        return UsageSummary(
            total_cost_usd=summary_data.get("total_cost_usd", Decimal("0.00")),
            total_requests=summary_data.get("total_requests", 0),
            providers={},
            models={},
            period_start=summary_data.get("period_start") or start_date or datetime.now(timezone.utc),
            period_end=summary_data.get("period_end") or end_date or datetime.now(timezone.utc),
        )
