"""Repository for LLM rate card management."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditAction, AuditEntityType
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


# ONE definition of "this rate card prices this model name" and "this entry is in force", shared by
# every query that answers it: the billing lookups (get_active_rate / get_all_active_rates) and the
# coverage check (find_card_providers_for_models). Keeping them apart is how the provider check ended
# up reporting a model as uncovered while billing priced it from a pattern card — or the reverse.
# Only identifiers/binds already in the surrounding query are interpolated; never user input.
def _model_match(model_expr: str, rc: str = "rc") -> str:
    """Exact name when the card has no pattern, POSIX regex when it does."""
    return (
        f"(({rc}.model_name_pattern IS NULL AND {rc}.model_name = {model_expr})"
        f" OR ({rc}.model_name_pattern IS NOT NULL AND {model_expr} ~ {rc}.model_name_pattern))"
    )


def _entry_in_force(as_of_expr: str = "NOW()", rce: str = "rce") -> str:
    """The entry is effective at ``as_of``: started, and not yet expired.

    Note this is NOT ``effective_until IS NULL``: a scheduled price change closes the current entry
    with a FUTURE ``effective_until``, and that entry is still what bills today.
    """
    return f"({rce}.effective_from <= {as_of_expr} AND ({rce}.effective_until IS NULL OR {rce}.effective_until > {as_of_expr}))"


class RateCardRepository(AuditedRepository):
    """Repository for managing LLM cost rate cards with audit trail."""

    def __init__(self):
        super().__init__(
            entity_type=AuditEntityType.RATE_CARD,
            table_name="rate_cards",  # Parent table for audit logging
        )

    async def _find_rate_card_id(
        self,
        db: AsyncSession,
        provider: str,
        model_name: str,
        *,
        for_update: bool = False,
    ) -> int | None:
        """The card id at the (provider, model_name) key — uq_rate_card makes it unique — or None.

        ``for_update`` row-locks it, for read-then-write flows (re-key) that must not interleave.
        """
        query = text(
            "SELECT id FROM rate_cards WHERE provider = :provider AND model_name = :model_name"
            + (" FOR UPDATE" if for_update else "")
        )
        result = await db.execute(query, {"provider": provider, "model_name": model_name})
        row = result.mappings().first()
        return row["id"] if row else None

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
        existing_id = await self._find_rate_card_id(db, provider, model_name)

        if existing_id is not None:
            # Update pattern if provided and different
            if model_name_pattern is not None:
                update_query = text("""
                    UPDATE rate_cards
                    SET model_name_pattern = :pattern
                    WHERE id = :id AND (model_name_pattern IS NULL OR model_name_pattern != :pattern)
                """)
                await db.execute(update_query, {"id": existing_id, "pattern": model_name_pattern})
            return existing_id

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
        actor: User,
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
            actor: User creating the entry
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

        # No-op guard: if the current active rate for this card + billing unit already equals the
        # new price, skip the write. Re-registering/saving a model with unchanged prices would
        # otherwise insert an identical superseding version every time, bloating the history (the
        # "current" rate is the latest effective_from, so duplicates are silent but accumulate).
        current = await db.execute(
            text("""
                SELECT id, price_per_million
                FROM rate_card_entries
                WHERE rate_card_id = :rate_card_id
                  AND billing_unit = :billing_unit
                  AND effective_from <= :effective_from
                  AND (effective_until IS NULL OR effective_until > :effective_from)
                ORDER BY effective_from DESC
                LIMIT 1
            """),
            {"rate_card_id": rate_card_id, "billing_unit": billing_unit, "effective_from": effective_from},
        )
        existing = current.mappings().first()
        if existing is not None and Decimal(str(existing["price_per_million"])) == Decimal(str(price_per_million)):
            logger.debug(
                "Rate unchanged for %s/%s %s (%s) — skipping duplicate entry",
                provider,
                model_name,
                billing_unit,
                price_per_million,
            )
            return existing["id"]

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
            actor=actor,
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
        actor: User,
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
            actor: User creating the entries
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
                actor=actor,
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
            f"effective from {effective_from} by {actor.sub}"
        )

        return entry_ids

    async def find_card_providers_for_models(
        self,
        db: AsyncSession,
        models: list[str],
    ) -> dict[str, dict[str, bool]]:
        """Per model name, the providers whose active rate card prices it → and is that card safe to
        re-key for this name (``True``) or not (``False``)?

        ONE query for the whole gateway fleet instead of a ``get_all_active_rates`` round-trip per
        deployment (the config check runs on every admin page mount), and it answers every question
        the check has: is this deployment's runtime provider covered (is it in the mapping), which
        other keys price this model (the rest), and which of those a re-key may actually move.

        Pattern cards and scheduled (closed-ended) entries must count for "is it priced" — they are
        exactly what billing resolves (``_model_match``/``_entry_in_force`` are shared with
        ``get_active_rate``). They are NOT movable, which is a different question: ``rekey`` moves a
        card HEADER identified by ``(provider, model_name)``, so a pattern card matching this name
        while keyed on another one would 404, and even one keyed on this very name prices every other
        model its regex covers — moving it would silently un-bill those. Only a plain, exact-name card
        is offered; a pattern is an explicit decision on the Rate Cards page.

        Names with no active card at all are absent from the result (not an empty dict).
        """
        if not models:
            return {}
        query = text(f"""
            SELECT c.model_name,
                   rc.provider,
                   bool_or(rc.model_name_pattern IS NULL AND rc.model_name = c.model_name) AS movable
            FROM unnest(CAST(:models AS text[])) AS c(model_name)
            JOIN rate_cards rc ON {_model_match("c.model_name")}
            WHERE EXISTS (
                SELECT 1 FROM rate_card_entries rce
                WHERE rce.rate_card_id = rc.id AND {_entry_in_force()}
            )
            GROUP BY c.model_name, rc.provider
        """)
        result = await db.execute(query, {"models": list(dict.fromkeys(models))})
        providers: dict[str, dict[str, bool]] = {}
        for row in result.mappings():
            providers.setdefault(row["model_name"], {})[row["provider"]] = bool(row["movable"])
        return providers

    async def find_orphan_card_providers(
        self,
        db: AsyncSession,
        families: list[str],
    ) -> list[dict]:
        """Active rate cards keyed on a provider outside ``families`` — pricing nothing can match.

        The runtime only ever stamps a provider family on usage, so a card under any other
        vocabulary (a LiteLLM catalog tag like ``bedrock_converse``, a Vertex location like ``eu``, a
        hand-typed ``bedrock-anthropic``) is dead pricing: whatever it was meant to bill is billing
        $0. Unlike the usage audit this needs no traffic to find them, and unlike the deployment
        check it needs no gateway. The vocabulary is passed in — the repository holds no opinion on
        which providers exist (see ``runtime_provider_families``).
        """
        query = text(f"""
            SELECT rc.provider, rc.model_name, rc.model_name_pattern
            FROM rate_cards rc
            WHERE rc.provider <> ALL(CAST(:families AS text[]))
              AND EXISTS (
                  SELECT 1 FROM rate_card_entries rce
                  WHERE rce.rate_card_id = rc.id AND {_entry_in_force()}
              )
            ORDER BY rc.provider, rc.model_name
        """)
        result = await db.execute(query, {"families": families})
        return [dict(row) for row in result.mappings()]

    async def rekey_model_provider(
        self,
        db: AsyncSession,
        actor: User,
        model_name: str,
        from_provider: str,
        to_provider: str,
    ) -> int:
        """Re-key a rate card to the provider billing actually reports (audited).

        Moves the card header, so its whole pricing history follows — no price re-entry.
        Raises LookupError when no card exists at (from_provider, model_name) and ValueError
        when one already exists at the target key (uq_rate_card would be violated; merging
        two histories is a manual decision, not something to do implicitly).
        """
        # Lock the source card so two admins clicking the same banner button serialize instead of
        # both moving it.
        rate_card_id = await self._find_rate_card_id(db, from_provider, model_name, for_update=True)
        if rate_card_id is None:
            raise LookupError(f"No rate card for {from_provider}/{model_name}")

        conflict = (
            f"A rate card for {to_provider}/{model_name} already exists; "
            "merge or remove one of the two manually."
        )
        if await self._find_rate_card_id(db, to_provider, model_name) is not None:
            raise ValueError(conflict)

        # The check above is check-then-act: a concurrent create/re-key (or a registration of the
        # same model) can claim the target key between the SELECT and this UPDATE. uq_rate_card is
        # the real guard, so run the UPDATE in a savepoint and translate its violation into the
        # same 409 ValueError — otherwise the race surfaces as an unhandled 500 with a raw DB error.
        try:
            async with db.begin_nested():
                await db.execute(
                    text("UPDATE rate_cards SET provider = :to_provider, updated_at = NOW() WHERE id = :id"),
                    {"to_provider": to_provider, "id": rate_card_id},
                )
        except IntegrityError as e:
            raise ValueError(conflict) from e

        await self.audit_service.log_action(
            db=db,
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type=AuditEntityType.RATE_CARD,
            entity_id=str(rate_card_id),
            changes={
                "before": {"provider": from_provider, "model_name": model_name},
                "after": {"provider": to_provider, "model_name": model_name},
            },
        )

        logger.info(
            "Re-keyed rate card %s for %s: %s -> %s by %s",
            rate_card_id,
            model_name,
            from_provider,
            to_provider,
            actor.sub,
        )
        return rate_card_id

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
        query = text(f"""
            SELECT rce.price_per_million
            FROM rate_card_entries rce
            JOIN rate_cards rc ON rc.id = rce.rate_card_id
            WHERE rc.provider = :provider
              AND {_model_match(":model_name")}
              AND rce.billing_unit = :billing_unit
              AND {_entry_in_force(":as_of")}
            ORDER BY
              CASE WHEN rc.model_name_pattern IS NULL THEN 0 ELSE 1 END,  -- exact beats pattern
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
        query = text(f"""
            SELECT DISTINCT ON (rce.billing_unit)
                rce.billing_unit,
                rce.price_per_million
            FROM rate_card_entries rce
            JOIN rate_cards rc ON rc.id = rce.rate_card_id
            WHERE rc.provider = :provider
              AND {_model_match(":model_name")}
              AND {_entry_in_force(":as_of")}
            ORDER BY rce.billing_unit,
              CASE WHEN rc.model_name_pattern IS NULL THEN 0 ELSE 1 END,  -- exact beats pattern
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
        actor: User,
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
            actor=actor,
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
        actor: User,
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
            actor: User performing the copy
            source_provider: Source provider name
            source_model: Source model name
            target_provider: Target provider name
            target_model: Target model name
            target_model_pattern: Target model name pattern
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
            actor=actor,
            provider=target_provider,
            model_name=target_model,
            model_name_pattern=target_model_pattern,
            pricing=source_rates,
            effective_from=effective_from,
        )
