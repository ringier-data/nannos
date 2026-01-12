"""Repository for LLM rate card management."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from .base import AuditedRepository

logger = logging.getLogger(__name__)


class RateCardRepository(AuditedRepository):
    """Repository for managing LLM cost rate cards with audit trail."""

    def __init__(self):
        super().__init__(
            entity_type=AuditEntityType.RATE_CARD,
            table_name="rate_cards",  # Parent table for audit logging
        )

    async def _get_or_create_rate_card_id(
        self,
        db: AsyncSession,
        provider: str,
        model_name: str,
        model_name_pattern: str | None = None,
    ) -> int:
        """
        Get the rate_card_id for a provider/model, creating if needed.

        Args:
            db: Database session
            provider: Provider name
            model_name: Model name
            model_name_pattern: Optional regex pattern for matching model variants

        Returns:
            rate_card_id
        """
        # Try to get existing
        query = text("""
            SELECT id FROM rate_cards
            WHERE provider = :provider AND model_name = :model_name
        """)
        result = await db.execute(query, {"provider": provider, "model_name": model_name})
        row = result.mappings().first()

        if row:
            # Update pattern if provided and different
            if model_name_pattern is not None:
                update_query = text("""
                    UPDATE rate_cards
                    SET model_name_pattern = :pattern
                    WHERE id = :id AND (model_name_pattern IS NULL OR model_name_pattern != :pattern)
                """)
                await db.execute(update_query, {"id": row["id"], "pattern": model_name_pattern})
            return row["id"]

        # Create new
        insert = text("""
            INSERT INTO rate_cards (provider, model_name, model_name_pattern)
            VALUES (:provider, :model_name, :pattern)
            RETURNING id
        """)
        result = await db.execute(
            insert, {"provider": provider, "model_name": model_name, "pattern": model_name_pattern}
        )
        await db.flush()
        rate_card_id = result.scalar()
        if rate_card_id is None:
            raise ValueError("Failed to create rate card")
        return rate_card_id

    async def create_entry(
        self,
        db: AsyncSession,
        actor_sub: str,
        provider: str,
        model_name: str,
        billing_unit: str,
        flow_direction: str,
        price_per_million: Decimal,
        effective_from: datetime,
        model_name_pattern: str | None = None,
    ) -> int:
        """
        Create a new rate card entry.

        Args:
            db: Database session
            actor_sub: User creating the entry
            provider: Provider name (e.g., 'bedrock', 'openai')
            model_name: Model name (e.g., 'claude-sonnet-4.5')
            billing_unit: Billing unit (e.g., 'input_tokens', 'requests')
            flow_direction: Flow direction ('input', 'output', or 'other')
            price_per_million: Price per million units in USD
            effective_from: When this rate becomes effective
            model_name_pattern: Optional regex pattern for matching model variants

        Returns:
            ID of created rate card entry
        """
        # Get or create rate_card
        rate_card_id = await self._get_or_create_rate_card_id(db, provider, model_name, model_name_pattern)

        # Insert entry
        insert = text("""
            INSERT INTO rate_card_entries (
                rate_card_id, billing_unit, flow_direction, price_per_million, effective_from
            )
            VALUES (:rate_card_id, :billing_unit, :flow_direction, :price_per_million, :effective_from)
            RETURNING id
        """)
        result = await db.execute(
            insert,
            {
                "rate_card_id": rate_card_id,
                "billing_unit": billing_unit,
                "flow_direction": flow_direction,
                "price_per_million": price_per_million,
                "effective_from": effective_from,
            },
        )
        await db.flush()
        entry_id = result.scalar()
        if entry_id is None:
            raise ValueError("Failed to create rate card entry")

        # Log audit
        await self.audit_service.log_action(
            db=db,
            actor_sub=actor_sub,
            action=AuditAction.CREATE,
            entity_type=AuditEntityType.RATE_CARD,
            entity_id=str(rate_card_id),
            changes={
                "after": {
                    "provider": provider,
                    "model_name": model_name,
                    "entry": {
                        "id": entry_id,
                        "billing_unit": billing_unit,
                        "flow_direction": flow_direction,
                        "price_per_million": float(price_per_million),
                        "effective_from": effective_from.isoformat(),
                    },
                }
            },
        )

        return entry_id

    async def create_model_rate_card(
        self,
        db: AsyncSession,
        actor_sub: str,
        provider: str,
        model_name: str,
        model_name_pattern: str | None,
        pricing: dict[str, Any],  # Can be dict or RateCardPricingEntry
        effective_from: datetime,
    ) -> list[int]:
        """
        Create all rate card entries for a model at once.

        Args:
            db: Database session
            actor_sub: User creating the entries
            provider: Provider name
            model_name: Model name
            model_name_pattern: Optional regex pattern for matching model variants
            pricing: Mapping of billing_unit to pricing details (dict with 'price_per_million' and 'flow_direction')
            effective_from: When these rates become effective

        Returns:
            List of created rate card entry IDs
        """
        entry_ids = []

        for billing_unit, pricing_entry in pricing.items():
            # Handle both old dict format (Decimal) and new format (RateCardPricingEntry)
            if isinstance(pricing_entry, dict):
                price = pricing_entry.get("price_per_million")
                flow_direction = pricing_entry.get("flow_direction", "other")
            else:
                # Pydantic model (RateCardPricingEntry)
                price = pricing_entry.price_per_million
                flow_direction = pricing_entry.flow_direction

            if price is None:
                raise ValueError(f"Missing price_per_million for billing_unit {billing_unit}")

            entry_id = await self.create_entry(
                db=db,
                actor_sub=actor_sub,
                provider=provider,
                model_name=model_name,
                billing_unit=billing_unit,
                flow_direction=flow_direction,
                price_per_million=price,
                effective_from=effective_from,
                model_name_pattern=model_name_pattern,
            )
            entry_ids.append(entry_id)

        logger.info(
            f"Created {len(entry_ids)} rate card entries for {provider}/{model_name} "
            f"effective from {effective_from} by {actor_sub}"
        )

        return entry_ids

    async def get_active_rate(
        self,
        db: AsyncSession,
        provider: str,
        model_name: str,
        billing_unit: str,
        as_of: datetime | None = None,
    ) -> Decimal | None:
        """
        Get the active rate for a specific billing unit.
        Uses exact match for rate cards without pattern, regex match for rate cards with pattern.

        Args:
            db: Database session
            provider: Provider name
            model_name: Model name
            billing_unit: Billing unit name
            as_of: Date to check rate at (defaults to now)

        Returns:
            Price per million units, or None if no rate found
        """
        if as_of is None:
            as_of = datetime.now(timezone.utc)

        # Single query that handles both exact and pattern matching
        query = text("""
            SELECT rce.price_per_million
            FROM rate_card_entries rce
            JOIN rate_cards rc ON rc.id = rce.rate_card_id
            WHERE rc.provider = :provider
              AND (
                (rc.model_name_pattern IS NULL AND rc.model_name = :model_name)
                OR
                (rc.model_name_pattern IS NOT NULL AND :model_name ~ rc.model_name_pattern)
              )
              AND rce.billing_unit = :billing_unit
              AND rce.effective_from <= :as_of
              AND (rce.effective_until IS NULL OR rce.effective_until > :as_of)
            ORDER BY 
              CASE WHEN rc.model_name_pattern IS NULL THEN 0 ELSE 1 END,  -- Prefer exact matches
              rce.effective_from DESC
            LIMIT 1
        """)

        result = await db.execute(
            query,
            {
                "provider": provider,
                "model_name": model_name,
                "billing_unit": billing_unit,
                "as_of": as_of,
            },
        )
        row = result.mappings().first()

        return Decimal(str(row["price_per_million"])) if row else None

    async def get_all_active_rates(
        self,
        db: AsyncSession,
        provider: str,
        model_name: str,
        as_of: datetime | None = None,
    ) -> dict[str, Decimal]:
        """
        Get all active rates for a model.
        Uses exact match for rate cards without pattern, regex match for rate cards with pattern.

        Args:
            db: Database session
            provider: Provider name
            model_name: Model name
            as_of: Date to check rates at (defaults to now)

        Returns:
            Mapping of billing_unit to price_per_million
        """
        if as_of is None:
            as_of = datetime.now(timezone.utc)

        # Single query that handles both exact and pattern matching
        query = text("""
            SELECT DISTINCT ON (rce.billing_unit) 
                rce.billing_unit,
                rce.price_per_million
            FROM rate_card_entries rce
            JOIN rate_cards rc ON rc.id = rce.rate_card_id
            WHERE rc.provider = :provider
              AND (
                (rc.model_name_pattern IS NULL AND rc.model_name = :model_name)
                OR
                (rc.model_name_pattern IS NOT NULL AND :model_name ~ rc.model_name_pattern)
              )
              AND rce.effective_from <= :as_of
              AND (rce.effective_until IS NULL OR rce.effective_until > :as_of)
            ORDER BY rce.billing_unit, 
              CASE WHEN rc.model_name_pattern IS NULL THEN 0 ELSE 1 END,  -- Prefer exact matches
              rce.effective_from DESC
        """)

        result = await db.execute(
            query,
            {"provider": provider, "model_name": model_name, "as_of": as_of},
        )

        return {row["billing_unit"]: Decimal(str(row["price_per_million"])) for row in result.mappings()}

    async def list_models_with_rates(
        self,
        db: AsyncSession,
        provider: str | None = None,
    ) -> list[dict[str, str]]:
        """
        List all models that have rate cards.

        Args:
            db: Database session
            provider: Optional provider filter

        Returns:
            List of dicts with 'provider' and 'model_name'
        """
        where_clause = "WHERE EXISTS (SELECT 1 FROM rate_card_entries rce WHERE rce.rate_card_id = rc.id AND rce.effective_until IS NULL)"
        if provider:
            where_clause += " AND rc.provider = :provider"

        query = text(f"""
            SELECT DISTINCT rc.provider, rc.model_name
            FROM rate_cards rc
            {where_clause}
            ORDER BY rc.provider, rc.model_name
        """)

        params = {"provider": provider} if provider else {}
        result = await db.execute(query, params)

        return [dict(row) for row in result.mappings()]

    async def list_entries(
        self,
        db: AsyncSession,
        provider: str | None = None,
        model_name: str | None = None,
        active_only: bool = True,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[dict], int]:
        """
        List rate card entries with pagination.

        Args:
            db: Database session
            provider: Optional provider filter
            model_name: Optional model name filter
            active_only: Only return currently active rates
            page: Page number (1-indexed)
            limit: Items per page

        Returns:
            Tuple of (entries, total_count)
        """
        where_conditions = []
        params: dict[str, Any] = {"limit": limit, "offset": (page - 1) * limit}

        if provider:
            where_conditions.append("rc.provider = :provider")
            params["provider"] = provider

        if model_name:
            where_conditions.append("rc.model_name = :model_name")
            params["model_name"] = model_name

        if active_only:
            where_conditions.append("rce.effective_until IS NULL")

        where_clause = ""
        if where_conditions:
            where_clause = "WHERE " + " AND ".join(where_conditions)

        # Get total count
        count_query = text(f"""
            SELECT COUNT(*) as total
            FROM rate_card_entries rce
            JOIN rate_cards rc ON rc.id = rce.rate_card_id
            {where_clause}
        """)
        count_result = await db.execute(count_query, params)
        total = count_result.scalar()
        if total is None:
            total = 0

        # Get entries with provider and model_name joined
        query = text(f"""
            SELECT 
                rce.id,
                rc.provider,
                rc.model_name,
                rc.model_name_pattern,
                rce.billing_unit,
                rce.flow_direction,
                rce.price_per_million,
                rce.effective_from,
                rce.effective_until,
                rce.created_at,
                rce.updated_at
            FROM rate_card_entries rce
            JOIN rate_cards rc ON rc.id = rce.rate_card_id
            {where_clause}
            ORDER BY rc.provider, rc.model_name, rce.billing_unit, rce.effective_from DESC
            LIMIT :limit OFFSET :offset
        """)

        result = await db.execute(query, params)
        entries = [dict(row) for row in result.mappings()]

        return entries, total

    async def expire_rate(
        self,
        db: AsyncSession,
        actor_sub: str,
        rate_id: int,
        effective_until: datetime,
    ) -> None:
        """
        Expire a rate card entry by setting effective_until.

        Args:
            db: Database session
            actor_sub: User expiring the rate
            rate_id: Rate card entry ID
            effective_until: When this rate should stop being effective
        """
        # Get entry and rate_card info for audit
        query = text("""
            SELECT rce.*, rc.provider, rc.model_name, rc.id as rate_card_id
            FROM rate_card_entries rce
            JOIN rate_cards rc ON rc.id = rce.rate_card_id
            WHERE rce.id = :rate_id
        """)
        result = await db.execute(query, {"rate_id": rate_id})
        entry_before = result.mappings().first()

        if not entry_before:
            raise ValueError(f"Rate card entry {rate_id} not found")

        # Update entry
        update = text("""
            UPDATE rate_card_entries
            SET effective_until = :effective_until, updated_at = :updated_at
            WHERE id = :rate_id
        """)
        await db.execute(
            update,
            {"rate_id": rate_id, "effective_until": effective_until, "updated_at": datetime.now(timezone.utc)},
        )
        await db.flush()

        # Log audit
        await self.audit_service.log_action(
            db=db,
            actor_sub=actor_sub,
            action=AuditAction.UPDATE,
            entity_type=AuditEntityType.RATE_CARD,
            entity_id=str(entry_before["rate_card_id"]),
            changes={
                "before": {
                    "entry_id": rate_id,
                    "effective_until": entry_before["effective_until"].isoformat()
                    if entry_before["effective_until"]
                    else None,
                },
                "after": {
                    "entry_id": rate_id,
                    "effective_until": effective_until.isoformat(),
                },
            },
        )

    async def copy_model_rates(
        self,
        db: AsyncSession,
        actor_sub: str,
        source_provider: str,
        source_model: str,
        target_provider: str,
        target_model: str,
        target_model_pattern: str | None,
        effective_from: datetime,
    ) -> list[int]:
        """
        Copy all rate card entries from one model to another.

        Args:
            db: Database session
            actor_sub: User performing the copy
            source_provider: Source provider name
            source_model: Source model name
            target_provider: Target provider name
            target_model: Target model name
            effective_from: When the new rates become effective

        Returns:
            List of created rate card entry IDs
        """
        # Get all active rates from source
        source_rates = await self.get_all_active_rates(db, source_provider, source_model)

        if not source_rates:
            raise ValueError(f"No active rates found for {source_provider}/{source_model}")

        # Create entries for target
        return await self.create_model_rate_card(
            db=db,
            actor_sub=actor_sub,
            provider=target_provider,
            model_name=target_model,
            model_name_pattern=target_model_pattern,
            pricing=source_rates,
            effective_from=effective_from,
        )
