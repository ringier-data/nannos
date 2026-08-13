"""Service for managing LLM cost rate cards."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..models.user import User
from ..repositories.rate_card_repository import RateCardRepository

logger = logging.getLogger(__name__)


def runtime_billing_provider(litellm_params: dict) -> str | None:
    """The provider key the cost logger will stamp on a deployment's usage rows.

    Mirrors litellm-proxy/custom_logger.py::_build_record — ``custom_llm_provider`` else the
    provider-route prefix of the model id — because rate cards MUST be keyed on that runtime
    value or every call bills $0 (see AGENTS.md "Rate-Card Provider Must Equal the litellm
    Provider Family"). The gateway's ``model_info.litellm_provider`` is NOT usable for this: it
    is LiteLLM's cost-map *implementation tag* (``bedrock_converse``,
    ``vertex_ai-language-models``), a different vocabulary from the runtime family (``bedrock``,
    ``vertex_ai``) the logger emits. None when underivable (unprefixed model id and no
    custom_llm_provider) — callers must reject or skip, not guess.
    """
    provider = litellm_params.get("custom_llm_provider")
    if provider:
        return str(provider)
    model = str(litellm_params.get("model") or "")
    if "/" in model:
        return model.split("/", 1)[0]
    return None


# LiteLLM tags catalog models by *implementation* (`bedrock_converse`, `vertex_ai-anthropic_models`)
# but routes and cost-logs by *family* (`bedrock`, `vertex_ai`) — get_llm_provider normalizes
# tag→family internally, as a hardcoded if/elif chain in litellm source, not data we can fetch
# (verified on litellm 1.90.0; the router does NOT store the resolved provider on deployments
# either). The two constants below are the minimal mirror of that normalization: tags whose family
# differs from the tag itself, and the families verified to route under their own name.
_TAG_TO_FAMILY = {"bedrock_converse": "bedrock"}
_VERTEX_TAG_PREFIX = "vertex_ai"  # vertex_ai-anthropic_models, vertex_ai-language-models, …
_BUILTIN_FAMILIES = {"bedrock", "vertex_ai", "azure", "azure_ai", "gemini", "openai"}


def runtime_provider_families() -> set[str]:
    """Every provider family this deployment can legitimately key a rate card on.

    The verified built-ins plus whatever LLM_GATEWAY_PROVIDERS adds
    (``config.model_gateway.integrated_providers``), so a deployment that integrates another vendor
    (say `mistral`) can register and bill it without a code change — for those, tag == family,
    which is exactly how litellm routes `mistral/…`. Tags carrying an implementation suffix are
    excluded: ``route_family`` normalizes those, they are never families themselves.
    """
    configured = {
        p
        for p in config.model_gateway.integrated_providers
        if p not in _TAG_TO_FAMILY and not p.startswith(_VERTEX_TAG_PREFIX)
    }
    return _BUILTIN_FAMILIES | configured


def route_family(catalog_tag: str | None) -> str | None:
    """Runtime provider family of a LiteLLM catalog tag, or None when it isn't one.

    Used ONLY to auto-prefix unprefixed catalog model ids at registration, so the deployment id,
    the runtime ``custom_llm_provider`` and the rate-card key agree by construction. Unknown tags
    resolve to None → registration 422s instead of guessing. Drift (a tag this misses, a litellm
    change) is caught by the rate-cards billing banner (provider_config_check), never billed silently.
    """
    if not catalog_tag:
        return None
    if catalog_tag in _TAG_TO_FAMILY:
        return _TAG_TO_FAMILY[catalog_tag]
    if catalog_tag.startswith(_VERTEX_TAG_PREFIX):
        return "vertex_ai"
    return catalog_tag if catalog_tag in runtime_provider_families() else None


async def resolve_deployment_provider(gateway_service: Any, litellm_params: dict) -> str | None:
    """The provider family a deployment bills under — the FULL rule, catalog step included.

    ``runtime_billing_provider`` alone is not that answer for a deployment: it reads the stored config,
    and an *unprefixed* model id is the norm for Bedrock (LiteLLM's cost map keys those bare, e.g.
    ``eu.anthropic.claude-haiku-4-5-20251001-v1:0``). At call time litellm resolves such an id through
    the same cost map and the logger stamps the family, so those deployments bill perfectly well —
    treating them as underivable would report working models as "will bill $0". The resolution is
    therefore: explicit prefix / ``custom_llm_provider``, else the server's own catalog entry for that
    exact id → ``route_family``. None only when neither knows it (a hand-typed id, a custom endpoint,
    or an unreadable catalog) — then nothing can bill it and callers must say so, not guess.

    ``gateway_service`` is passed in (not imported) to keep this next to the derivation rules it
    composes; it only needs ``catalog_model``, whose catalog is cached for hours.
    """
    provider = runtime_billing_provider(litellm_params)
    if provider:
        return provider
    model_id = str(litellm_params.get("model") or "")
    if not model_id:
        return None
    entry = await gateway_service.catalog_model(model_id)
    return route_family((entry or {}).get("provider"))


def is_catalog_tag_vocabulary(provider: str) -> bool:
    """True for values that are LiteLLM cost-map *tags*, never runtime providers.

    These are the vocabulary that provably cannot bill: `get_llm_provider` normalizes them away, and
    `bedrock_converse/…` isn't even a routable model-id prefix. Unlike "is it in our family list",
    this is a statement about LiteLLM, not about what this deployment happens to have integrated.
    """
    return provider in _TAG_TO_FAMILY or provider.startswith(f"{_VERTEX_TAG_PREFIX}-")


# Every rate-card write must satisfy one invariant: the card is keyed on the value the cost logger
# emits at call time. How much can be VERIFIED depends on where the value came from, and the two
# cases share no logic — hence two functions rather than one with a mode flag.


def assert_billable_provider(provider: str) -> None:
    """Reject an admin-typed rate-card provider key (raises ValueError).

    Checked against ``runtime_provider_families()``, because a typo and a wrong vocabulary
    (`bedrock-anthropic`, `eu`, `bedrock_converse`) are indistinguishable from a vendor we simply
    haven't integrated — the allowlist is the only guard available on a hand-entered value.
    """
    families = runtime_provider_families()
    if provider not in families:
        raise ValueError(
            f"'{provider}' is not a runtime billing provider. Rate cards must be keyed on the "
            f"provider family the cost logger reports ({', '.join(sorted(families))}) — LiteLLM "
            "catalog tags (e.g. 'bedrock_converse') and Vertex locations (e.g. 'eu') never match "
            "usage, so the model would silently bill $0. To bill another vendor, add its route to "
            "LLM_GATEWAY_PROVIDERS, or register the model so the route is derived from it."
        )


def assert_routable_provider(provider: str) -> None:
    """Reject a deployment-derived rate-card provider key (raises ValueError).

    Only the tag vocabulary is refused here. The value IS the deployment's own route — its model-id
    prefix or explicit ``custom_llm_provider`` — which is by construction what ``get_llm_provider``
    routes on and what the cost logger stamps, so any routable vendor is billable. Applying the
    family allowlist instead rejected legitimate `anthropic/…`, `groq/…`, `deepseek/…` with "not a
    runtime billing provider" immediately after the sibling error told the admin to add that very
    prefix. An unroutable prefix is caught by the mandatory post-registration test call, which rolls
    the registration back.
    """
    if is_catalog_tag_vocabulary(provider):
        raise ValueError(
            f"'{provider}' is a LiteLLM cost-map tag, not a provider route: it is not a routable "
            "model-id prefix and the cost logger never reports it, so the model would bill $0. "
            f"Use the route family it normalizes to ('{route_family(provider)}')."
        )


class RateCardService:
    """Service for managing rate cards and calculating LLM costs."""

    def __init__(self, rate_card_repository: RateCardRepository | None = None):
        """Initialize rate card service.

        Args:
            rate_card_repository: Optional rate card repository instance.
                If None, must be set via set_repository() before use.
        """
        self._repository = rate_card_repository
        self._rate_cache: dict[tuple[str, str, str], tuple[Decimal, datetime]] = {}
        self._cache_ttl_seconds = 300  # 5 minutes

    def set_repository(self, rate_card_repository: RateCardRepository) -> None:
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

    async def create_entry(
        self,
        db: AsyncSession,
        actor: User,
        provider: str,
        model_name: str,
        billing_unit: str,
        flow_direction: str,
        price_per_million: Decimal,
        effective_from: datetime | None = None,
        model_name_pattern: str | None = None,
    ) -> int:
        """Create one rate card entry (the Rate Cards page's single-unit write).

        Goes through the service — not straight to the repository — so the provider-keying
        invariant is enforced on this path too, exactly like the bulk/copy writes.
        """
        if effective_from is None:
            effective_from = datetime.now(timezone.utc)

        assert_billable_provider(provider)

        entry_id = await self.repository.create_entry(
            db=db,
            actor=actor,
            provider=provider,
            model_name=model_name,
            billing_unit=billing_unit,
            flow_direction=flow_direction,
            price_per_million=price_per_million,
            effective_from=effective_from,
            model_name_pattern=model_name_pattern,
        )
        self._invalidate_model_cache(provider, model_name)
        return entry_id

    async def create_model_rate_card(
        self,
        db: AsyncSession,
        actor: User,
        provider: str,
        model_name: str,
        pricing: dict[str, Any],  # Can be dict[str, Decimal] (old) or dict[str, RateCardPricingEntry] (new)
        effective_from: datetime | None = None,
        model_name_pattern: str | None = None,
        derived_from_deployment: bool = False,
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
            derived_from_deployment: True when the provider is the deployment's own route (register /
                edit resolved it from litellm_params) — validated by assert_routable_provider rather
                than the family allowlist. False for admin-typed values.

        Returns:
            List of created rate card entry IDs
        """
        if effective_from is None:
            effective_from = datetime.now(timezone.utc)

        if derived_from_deployment:
            assert_routable_provider(provider)
        else:
            assert_billable_provider(provider)

        entry_ids = await self.repository.create_model_rate_card(
            db=db,
            actor=actor,
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
        actor: User,
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
            actor: User performing the copy
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

        assert_billable_provider(target_provider)

        entry_ids = await self.repository.copy_model_rates(
            db=db,
            actor=actor,
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

    async def find_card_providers_for_models(
        self,
        db: AsyncSession,
        models: list[str],
    ) -> dict[str, dict[str, bool]]:
        """Per model name, provider → is its card movable (billing's own match rules).

        ``False`` marks a pattern card: it prices the name but is keyed elsewhere, so a re-key
        cannot move it. See ``RateCardRepository.find_card_providers_for_models``.
        """
        return await self.repository.find_card_providers_for_models(db, models)

    async def find_orphan_cards(self, db: AsyncSession) -> list[dict]:
        """Active rate cards keyed outside the runtime vocabulary — dead pricing, billing $0.

        The vocabulary lives here, not in the repository: it is the same
        ``runtime_provider_families()`` set every write path validates against, so a card this
        reports is exactly a card ``assert_billable_provider`` would refuse today.
        """
        return await self.repository.find_orphan_card_providers(db, sorted(runtime_provider_families()))

    async def rekey_model_provider(
        self,
        db: AsyncSession,
        actor: User,
        model_name: str,
        from_provider: str,
        to_provider: str,
    ) -> int:
        """Re-key a rate card to the runtime provider; the pricing history moves with it.

        The target key is validated like every other rate-card write: a re-key onto a non-runtime
        vocabulary would move a working card onto a key usage never matches — the exact $0-billing
        state the whole provider check exists to find, reachable in one API call.
        """
        assert_billable_provider(to_provider)

        rate_card_id = await self.repository.rekey_model_provider(
            db=db,
            actor=actor,
            model_name=model_name,
            from_provider=from_provider,
            to_provider=to_provider,
        )
        # Old-key hits linger in the rate cache up to its TTL; drop both keys so billing
        # switches to the corrected card immediately.
        self._invalidate_model_cache(from_provider, model_name)
        self._invalidate_model_cache(to_provider, model_name)
        return rate_card_id

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
