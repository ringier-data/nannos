"""Is billing *configured* correctly right now — the rate-card ↔ runtime-provider invariant.

Rate cards must be keyed on the provider family the cost logger stamps at call time, or every call
bills $0 (AGENTS.md "Rate-Card Provider Must Equal the litellm Provider Family"). This check answers
that question from configuration alone, in two directions:

- forward: every gateway deployment's runtime provider must have an active card pricing its alias
  (``resolve_deployment_provider`` — the cost logger's own rule plus the catalog step, i.e. exactly
  what registration resolves — and billing's own exact-or-pattern card match). Catches a mis-keyed
  model BEFORE its first call;
- backward: no active card may be keyed outside ``runtime_provider_families()``. Catches dead
  pricing (catalog tags, Vertex locations, hand-typed vendors) with no traffic and no gateway.

Deterministic, cheap (one already-cached gateway list + two point queries) and always actionable:
every finding is a live misconfiguration, so a healthy system reports nothing. That is why there is
no result cache and no time window here: the answer is knowable instantly and must be right the
moment a fix lands. Calls that ALREADY billed $0 are a different question — computed at ingest and
not retroactively fixable — so they belong in usage reporting, not in this check.
"""

import logging
from typing import TYPE_CHECKING, Any

from ..models.usage import OrphanCard, ProviderConfigCheck, UnbillableDeployment
from .rate_card_service import resolve_deployment_provider

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def derive_alias_providers(gateway_service: Any, gateway_models: list[dict]) -> dict[str, set[str]]:
    """alias → the runtime provider(s) its deployments will bill under.

    Uses ``resolve_deployment_provider``, i.e. the same resolution registration uses, INCLUDING the
    catalog step. Reading only the stored routing params would report every unprefixed Bedrock
    deployment as unbillable — and those are the norm (LiteLLM's cost map keys Bedrock models bare)
    and demonstrably bill fine: litellm resolves the id through that same cost map at call time and
    the logger stamps ``bedrock`` on the usage row.

    An alias whose id nothing resolves contributes an EMPTY set: it is registered, and nothing can
    bill it.
    """
    alias_derived: dict[str, set[str]] = {}
    for model in gateway_models:
        alias = model.get("model_name")
        if not alias:
            continue
        derived = await resolve_deployment_provider(gateway_service, model.get("litellm_params") or {})
        alias_derived.setdefault(alias, set())
        if derived:
            alias_derived[alias].add(derived)
    return alias_derived


async def _catalog_or_empty(gateway_service: Any) -> list[dict]:
    """The catalog, or [] when it can't be read (it is only used as a "can we resolve at all" signal).

    ``get_catalog`` already degrades to a stale cache and then to [], so an exception here would be a
    surprise — but this runs on an admin page load, and a broken catalog must not 500 the check.
    """
    try:
        return await gateway_service.get_catalog()
    except Exception as e:
        logger.warning(f"Provider config check: catalog unreadable, unresolved routes not flagged: {e}")
        return []


async def check_provider_config(request: "Request", db: "AsyncSession") -> ProviderConfigCheck:
    """Every deployment that can't bill, and every card that can't be billed against."""
    rate_card_service = request.app.state.rate_card_service

    gateway_checked = True
    try:
        gateway_models = await request.app.state.model_gateway_service.list_models()
    except Exception as e:  # orphan cards need no gateway; report those rather than nothing
        logger.warning(f"Provider config check: gateway unreachable, deployment half skipped: {e}")
        gateway_models = []
        gateway_checked = False

    gateway_service = request.app.state.model_gateway_service
    alias_derived = await derive_alias_providers(gateway_service, gateway_models)
    # An unprefixed id is resolved through the catalog, and get_catalog fails SOFT (stale cache, else
    # []). With no catalog we cannot tell a bare-but-valid Bedrock id from an unroutable one — so a
    # catalog outage must not turn every such deployment into a red "will bill $0" row. Report only
    # what we could resolve; say nothing about the rest.
    catalog_readable = bool(await _catalog_or_empty(gateway_service))
    # One query for the whole fleet, with billing's own match semantics (pattern cards, scheduled
    # entries) — so "no card" here means exactly "get_active_rate would find nothing".
    card_providers = await rate_card_service.find_card_providers_for_models(db, sorted(alias_derived))

    unbillable: list[UnbillableDeployment] = []
    for alias in sorted(alias_derived):
        derived_set = alias_derived[alias]
        # provider → is its card a plain card keyed on THIS alias (vs. a pattern card, which may be
        # keyed on another name and prices a whole family). Both price the model; only the first is
        # safe to move, so the two answers must not be conflated.
        priced = card_providers.get(alias, {})
        priced_by = set(priced)
        movable = {provider for provider, is_movable in priced.items() if is_movable}
        if not derived_set:
            if not catalog_readable:
                continue  # unresolved because we couldn't look it up, not because it's unroutable
            # Nothing resolves this deployment's route — not its model id, not the catalog — so no
            # provider value exists to key a card on and litellm has nothing to route by either. Not
            # fixable by re-keying: the model id needs a route prefix (or a custom_llm_provider).
            unbillable.append(
                UnbillableDeployment(
                    model_name=alias,
                    runtime_provider=None,
                    other_providers=sorted(priced_by),
                    pattern_providers=sorted(priced_by - movable),
                    reason="provider_underivable",
                )
            )
            continue
        for derived in sorted(derived_set):
            if derived in priced_by:
                continue
            others = priced_by - {derived}
            unbillable.append(
                UnbillableDeployment(
                    model_name=alias,
                    runtime_provider=derived,
                    other_providers=sorted(others),
                    # Two exclusions, both because a re-key moves a card HEADER keyed on (provider,
                    # this alias): a card under a provider this alias is ALSO routed as is pricing
                    # that other deployment (moving it un-bills live traffic), and a pattern card is
                    # keyed on a different name entirely — the re-key would 404 and, if it didn't,
                    # would take the rest of that family's pricing with it. Both are still named.
                    rekey_candidates=sorted((movable & others) - derived_set),
                    pattern_providers=sorted(others - movable),
                    reason="card_under_other_provider" if others else "no_card",
                )
            )

    orphan_cards = [
        OrphanCard(
            provider=card["provider"],
            model_name=card["model_name"],
            model_name_pattern=card["model_name_pattern"],
        )
        for card in await rate_card_service.find_orphan_cards(db)
    ]

    return ProviderConfigCheck(
        unbillable_deployments=unbillable,
        orphan_cards=orphan_cards,
        gateway_checked=gateway_checked,
    )
