"""Service for managing LLM cost rate cards."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class RateCardService:
    """Service for managing rate cards and calculating LLM costs."""

    def __init__(self, rate_card_repository=None):
        """Initialize rate card service.

        Args:
            rate_card_repository: Optional rate card repository instance.
                If None, must be set via set_repository() before use.
        """
        self._repository = rate_card_repository
        self._rate_cache: dict[tuple[str, str, str], tuple[Decimal, datetime]] = {}
        self._cache_ttl_seconds = 300  # 5 minutes

    def set_repository(self, rate_card_repository):
        """Set the rate card repository (dependency injection)."""
        self._repository = rate_card_repository

    @property
    def repository(self):
        """Get the rate card repository, raising error if not set."""
        if self._repository is None:
            raise RuntimeError("RateCardRepository not injected. Call set_repository() during initialization.")
        return self._repository

    async def calculate_cost(
        self,
        db: AsyncSession,
        provider: str | None,
        model_name: str | None,
        billing_unit_breakdown: dict[str, int],
        as_of: datetime | None = None,
        sub_agent_config_version_id: int | None = None,
    ) -> Decimal:
        """
        Calculate cost from billing unit breakdown using rate cards.

        Args:
            db: Database session
            provider: Provider name (e.g., 'bedrock', 'openai')
            model_name: Model name (e.g., 'claude-sonnet-4.5')
            billing_unit_breakdown: Dict of billing_unit -> count
            as_of: Date to use for rate lookup (defaults to now)
            sub_agent_config_version_id: Optional config version ID for agent-specific pricing

        Returns:
            Total cost in USD

        Raises:
            ValueError: If no rate card found for a billing unit
        """
        if as_of is None:
            as_of = datetime.now(tz=timezone.utc)

        logger.info(
            f"calculate_cost called: provider={provider}, model_name={model_name}, "
            f"sub_agent_config_version_id={sub_agent_config_version_id}, billing_units={list(billing_unit_breakdown.keys())}"
        )

        # Fetch agent-specific pricing config if available
        agent_pricing_config = None
        if sub_agent_config_version_id:
            agent_pricing_config = await self._fetch_agent_pricing_config(db, sub_agent_config_version_id)
            if agent_pricing_config:
                logger.info(
                    f"Fetched agent-specific pricing config for sub_agent_config_version_id={sub_agent_config_version_id}: {agent_pricing_config}"
                )
            else:
                logger.warning(
                    f"No pricing_config found for sub_agent_config_version_id={sub_agent_config_version_id}, will use system rate cards"
                )

        total_cost = Decimal("0.00")
        missing_rates = []

        for billing_unit, count in billing_unit_breakdown.items():
            logger.debug(f"Calculating cost for billing_unit={billing_unit}, count={count}")
            if count <= 0:
                continue

            # Try agent-specific pricing first
            rate = None
            if agent_pricing_config:
                rate = self._get_agent_rate(agent_pricing_config, billing_unit)
                if rate is not None:
                    logger.info(
                        f"Using agent-specific rate for {billing_unit}: ${rate}/M (sub_agent_config_version_id={sub_agent_config_version_id})"
                    )
                else:
                    logger.warning(
                        f"Agent pricing config exists but no rate found for billing_unit={billing_unit}. "
                        f"Available entries: {[e.get('billing_unit') for e in agent_pricing_config.get('rate_card_entries', [])]}"
                    )

            # Fall back to system rate cards if no agent-specific rate
            if rate is None:
                # Only try system rate cards if provider and model_name are provided
                if provider is not None and model_name is not None:
                    if agent_pricing_config:
                        logger.debug(
                            f"No agent-specific rate found for {billing_unit} in sub_agent_config_version_id={sub_agent_config_version_id}. "
                            f"Falling back to system rate card for {provider}/{model_name}"
                        )

                    # Try cache first with exact billing unit
                    cache_key = (provider, model_name, billing_unit)
                    cached_rate, cached_at = self._rate_cache.get(cache_key, (None, None))

                    # Use cache if fresh
                    if cached_rate is not None and cached_at is not None:
                        age_seconds = (datetime.now(timezone.utc) - cached_at).total_seconds()
                        if age_seconds < self._cache_ttl_seconds:
                            rate = cached_rate
                        else:
                            rate = await self._fetch_and_cache_rate(db, provider, model_name, billing_unit, as_of)
                    else:
                        rate = await self._fetch_and_cache_rate(db, provider, model_name, billing_unit, as_of)

                    # If exact match failed, try fallback to base rates
                    if rate is None:
                        fallback_unit = self._get_fallback_billing_unit(billing_unit)
                        if fallback_unit and fallback_unit != billing_unit:
                            logger.info(
                                f"No rate found for {billing_unit}, falling back to {fallback_unit} "
                                f"for {provider}/{model_name}"
                            )
                            # Try cache for fallback unit
                            fallback_cache_key = (provider, model_name, fallback_unit)
                            cached_rate, cached_at = self._rate_cache.get(fallback_cache_key, (None, None))

                            if cached_rate is not None and cached_at is not None:
                                age_seconds = (datetime.now(timezone.utc) - cached_at).total_seconds()
                                if age_seconds < self._cache_ttl_seconds:
                                    rate = cached_rate
                                else:
                                    rate = await self._fetch_and_cache_rate(
                                        db, provider, model_name, fallback_unit, as_of
                                    )
                            else:
                                rate = await self._fetch_and_cache_rate(db, provider, model_name, fallback_unit, as_of)
                else:
                    # Cannot look up system rate cards without provider/model_name
                    if agent_pricing_config:
                        logger.warning(
                            f"No agent-specific rate found for {billing_unit} and provider/model_name not provided. "
                            f"Cannot fall back to system rate cards."
                        )
                    else:
                        logger.debug(
                            f"No agent-specific pricing config and provider/model_name not provided for {billing_unit}. "
                            f"Cannot look up rate card."
                        )

            if rate is None:
                missing_rates.append(billing_unit)
                logger.warning(
                    f"No rate card found for {provider}/{model_name}/{billing_unit} "
                    f"as of {as_of}. Skipping cost calculation for this billing unit."
                )
                continue

            # Calculate cost: (count / 1,000,000) * price_per_million
            unit_cost = (Decimal(str(count)) / Decimal("1000000")) * rate
            total_cost += unit_cost

            logger.debug(f"{provider}/{model_name}/{billing_unit}: {count} units × ${rate}/M = ${unit_cost:.8f}")

        if missing_rates:
            logger.error(
                f"Missing rate cards for {provider}/{model_name}: "
                f"{', '.join(missing_rates)}. Partial cost: ${total_cost}"
            )

        return total_cost.quantize(Decimal("0.00000001"))  # 8 decimal places

    def _get_fallback_billing_unit(self, billing_unit: str) -> str | None:
        """
        Get fallback billing unit for cost calculation when exact match not found.

        Fallback rules:
        - *_input_tokens (except base_input_tokens) → base_input_tokens
        - *_output_tokens (except base_output_tokens) → base_output_tokens

        Args:
            billing_unit: Original billing unit name

        Returns:
            Fallback billing unit name or None if no fallback applies
        """
        # Already base units - no fallback
        if billing_unit in ("base_input_tokens", "base_output_tokens"):
            return None

        # Input token variants fall back to base_input_tokens
        if billing_unit.endswith("_input_tokens"):
            return "base_input_tokens"

        # Output token variants fall back to base_output_tokens
        if billing_unit.endswith("_output_tokens"):
            return "base_output_tokens"

        # Non-token billing units and other patterns have no fallback
        return None

    async def _fetch_and_cache_rate(
        self,
        db: AsyncSession,
        provider: str,
        model_name: str,
        billing_unit: str,
        as_of: datetime,
    ) -> Decimal | None:
        """Fetch rate from database and update cache."""
        rate = await self.repository.get_active_rate(
            db=db,
            provider=provider,
            model_name=model_name,
            billing_unit=billing_unit,
            as_of=as_of,
        )

        if rate is not None:
            cache_key = (provider, model_name, billing_unit)
            self._rate_cache[cache_key] = (rate, datetime.now(timezone.utc))

        return rate

    async def validate_model_has_rates(
        self,
        db: AsyncSession,
        provider: str,
        model_name: str,
        required_billing_units: list[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """
        Validate that a model has complete rate cards.

        Args:
            db: Database session
            provider: Provider name
            model_name: Model name
            required_billing_units: Optional list of required billing units

        Returns:
            Tuple of (is_valid, missing_billing_units)
        """
        rates = await self.repository.get_all_active_rates(db, provider, model_name)

        if not rates:
            return False, required_billing_units or ["(no rates found)"]

        if required_billing_units:
            missing = [bu for bu in required_billing_units if bu not in rates]
            return len(missing) == 0, missing

        # If no specific requirements, just check we have some rates
        return True, []

    async def create_model_rate_card(
        self,
        db: AsyncSession,
        actor_sub: str,
        provider: str,
        model_name: str,
        pricing: dict[str, Any],  # Can be dict[str, Decimal] (old) or dict[str, RateCardPricingEntry] (new)
        effective_from: datetime | None = None,
        model_name_pattern: str | None = None,
    ) -> list[int]:
        """
        Create all rate card entries for a model.

        Args:
            db: Database session
            actor_sub: User creating the rate card
            provider: Provider name
            model_name: Model name
            pricing: Dict of billing_unit -> price details (Decimal for old format, RateCardPricingEntry for new)
            effective_from: When rates become effective (defaults to now)
            model_name_pattern: Optional regex pattern for matching model variants

        Returns:
            List of created rate card entry IDs
        """
        if effective_from is None:
            effective_from = datetime.now(timezone.utc)

        entry_ids = await self.repository.create_model_rate_card(
            db=db,
            actor_sub=actor_sub,
            provider=provider,
            model_name=model_name,
            model_name_pattern=model_name_pattern,
            pricing=pricing,
            effective_from=effective_from,
        )

        # Invalidate cache for this model
        self._invalidate_model_cache(provider, model_name)

        logger.info(
            f"Created rate card for {provider}/{model_name} with {len(pricing)} billing units, effective {effective_from}"
        )

        return entry_ids

    async def copy_model_rates(
        self,
        db: AsyncSession,
        actor_sub: str,
        source_provider: str,
        source_model: str,
        target_provider: str,
        target_model: str,
        target_model_pattern: str | None = None,
        effective_from: datetime | None = None,
    ) -> list[int]:
        """
        Copy rate card from one model to another.

        Args:
            db: Database session
            actor_sub: User performing the copy
            source_provider: Source provider
            source_model: Source model
            target_provider: Target provider
            target_model: Target model
            effective_from: When rates become effective (defaults to now)

        Returns:
            List of created rate card entry IDs
        """
        if effective_from is None:
            effective_from = datetime.now(timezone.utc)

        entry_ids = await self.repository.copy_model_rates(
            db=db,
            actor_sub=actor_sub,
            source_provider=source_provider,
            source_model=source_model,
            target_provider=target_provider,
            target_model=target_model,
            target_model_pattern=target_model_pattern,
            effective_from=effective_from,
        )

        # Invalidate cache for target model
        self._invalidate_model_cache(target_provider, target_model)

        logger.info(
            f"Copied rates from {source_provider}/{source_model} to "
            f"{target_provider}/{target_model}, {len(entry_ids)} entries created"
        )

        return entry_ids

    async def list_models_with_rates(
        self,
        db: AsyncSession,
        provider: str | None = None,
    ) -> list[dict[str, str]]:
        """
        List all models that have active rate cards.

        Args:
            db: Database session
            provider: Optional provider filter

        Returns:
            List of dicts with 'provider' and 'model_name'
        """
        return await self.repository.list_models_with_rates(db, provider)

    def _invalidate_model_cache(self, provider: str, model_name: str) -> None:
        """Invalidate all cached rates for a model."""
        keys_to_remove = [key for key in self._rate_cache.keys() if key[0] == provider and key[1] == model_name]

        for key in keys_to_remove:
            del self._rate_cache[key]

        if keys_to_remove:
            logger.debug(f"Invalidated {len(keys_to_remove)} rate cache entries for {provider}/{model_name}")

    def clear_cache(self) -> None:
        """Clear the entire rate cache."""
        self._rate_cache.clear()
        logger.info("Cleared rate card cache")

    async def _fetch_agent_pricing_config(self, db: AsyncSession, sub_agent_config_version_id: int) -> dict | None:
        """Fetch pricing_config from sub_agent_config_versions by version ID."""
        from sqlalchemy import text

        result = await db.execute(
            text("""
                SELECT pricing_config
                FROM sub_agent_config_versions
                WHERE id = :config_version_id
            """),
            {"config_version_id": sub_agent_config_version_id},
        )
        row = result.mappings().first()
        return row["pricing_config"] if row else None

    def _get_agent_rate(self, pricing_config: dict, billing_unit: str) -> Decimal | None:
        """Extract rate for a specific billing unit from agent pricing config.

        Format: {"rate_card_entries": [{"billing_unit": "input_tokens", "price_per_million": 1.5}]}
        """
        if not pricing_config:
            return None

        logger.debug(
            f"Looking up agent-specific rate for billing_unit={billing_unit} in pricing_config. {pricing_config}"
        )

        if "rate_card_entries" in pricing_config:
            for entry in pricing_config["rate_card_entries"]:
                if entry.get("billing_unit") == billing_unit:
                    price = entry.get("price_per_million")
                    if price is not None:
                        return Decimal(str(price))

        return None
