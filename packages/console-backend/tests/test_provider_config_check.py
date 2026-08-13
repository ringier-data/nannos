"""Provider config check: is billing configured to work, right now, from configuration alone.

The detection half of the rate-card provider invariant (the derivation and re-key rules live in
test_rate_card_provider_keying.py). No usage window, no cache, and every finding actionable — in
both directions:

- forward: a gateway deployment whose derived runtime provider no active card prices will bill $0
  from its first call, before any usage exists to notice it;
- backward: an active card keyed outside the runtime vocabulary is dead pricing, found with no
  traffic and no gateway at all.

"Priced" here must mean exactly what it means when billing resolves a rate — pattern cards and
scheduled (closed-ended) entries included — which is why the check and get_active_rate share one SQL
predicate.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from console_backend.models.user import User
from console_backend.repositories.rate_card_repository import RateCardRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.provider_config_check import check_provider_config
from console_backend.services.rate_card_service import RateCardService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _config_request(
    gateway_models,
    card_providers: dict[str, set[str] | dict[str, bool]] | None = None,
    orphans=None,
    catalog: list[dict] | None = None,
):
    # The repository reports provider → "is that card keyed on THIS name" (False = matched via a
    # pattern on another name). A plain set means "all exact", which is what most cases care about.
    priced = {
        model: (providers if isinstance(providers, dict) else dict.fromkeys(providers, True))
        for model, providers in (card_providers or {}).items()
    }
    rate_card_service = SimpleNamespace(
        find_card_providers_for_models=AsyncMock(return_value=priced),
        find_orphan_cards=AsyncMock(return_value=orphans or []),
    )
    entries = catalog or []
    gateway = SimpleNamespace(
        list_models=AsyncMock(side_effect=gateway_models)
        if callable(gateway_models)
        else AsyncMock(return_value=gateway_models),
        # Unprefixed ids are resolved through the server's own catalog, exactly as registration does.
        catalog_model=AsyncMock(side_effect=lambda mid: next((c for c in entries if c["model_id"] == mid), None)),
        # Non-empty by default so "unresolved" means unroutable, not unreadable. Tests that want the
        # catalog-outage path pass catalog=None and get [] here.
        get_catalog=AsyncMock(return_value=entries),
    )
    state = SimpleNamespace(model_gateway_service=gateway, rate_card_service=rate_card_service)
    return SimpleNamespace(app=SimpleNamespace(state=state))


# --- forward direction: deployments that can't bill ---


@pytest.mark.asyncio
async def test_deployment_without_any_card_is_flagged():
    request = _config_request(
        gateway_models=[
            {"model_name": "claude-new", "litellm_params": {"model": "bedrock/us.anthropic.claude-new-v1:0"}},
        ],
    )
    out = await check_provider_config(request, db=AsyncMock())

    (flagged,) = out.unbillable_deployments
    assert flagged.model_name == "claude-new"
    assert flagged.runtime_provider == "bedrock"
    assert flagged.reason == "no_card"
    assert flagged.rekey_candidates == []  # nothing to move
    assert out.gateway_checked is True


@pytest.mark.asyncio
async def test_card_under_the_wrong_key_is_offered_as_a_rekey():
    """The original incident, caught before the first call: the card is keyed on LiteLLM's cost-map
    tag while the deployment routes as the family."""
    request = _config_request(
        gateway_models=[
            {"model_name": "claude-opus-4-8", "litellm_params": {"model": "bedrock/us.anthropic.claude-opus-4-8-v1:0"}},
        ],
        card_providers={"claude-opus-4-8": {"bedrock_converse"}},
    )
    out = await check_provider_config(request, db=AsyncMock())

    (flagged,) = out.unbillable_deployments
    assert flagged.reason == "card_under_other_provider"
    assert flagged.other_providers == ["bedrock_converse"]
    assert flagged.rekey_candidates == ["bedrock_converse"]


@pytest.mark.asyncio
async def test_pattern_card_is_named_but_not_offered_as_a_rekey():
    """The reported case: alias `claude-haiku-4-5-personal` routes as `anthropic`, and the only card
    pricing it is `bedrock` / `claude-haiku-4.5` with pattern `^…claude-haiku-4-5.*$`. "Will bill $0" is
    right (no anthropic card), but offering "Re-key bedrock → anthropic" was not: /rekey targets
    (provider, model_name), so it 404s on a name no card is keyed on — and the Rate Cards table shows
    nothing for that name either, which is how it surfaced. Name it, explain it, don't offer it."""
    request = _config_request(
        gateway_models=[
            {
                "model_name": "claude-haiku-4-5-personal",
                "litellm_params": {"model": "anthropic/claude-haiku-4-5"},
            },
        ],
        card_providers={"claude-haiku-4-5-personal": {"bedrock": False}},  # priced, but via a pattern
    )
    out = await check_provider_config(request, db=AsyncMock())

    (flagged,) = out.unbillable_deployments
    assert flagged.runtime_provider == "anthropic"
    assert flagged.other_providers == ["bedrock"]  # explains why the model looks priced
    assert flagged.pattern_providers == ["bedrock"]  # …and why it can't be moved
    assert flagged.rekey_candidates == []  # no 404-ing button


@pytest.mark.asyncio
async def test_billable_deployment_is_not_flagged():
    request = _config_request(
        gateway_models=[
            {"model_name": "claude-opus-4-8", "litellm_params": {"model": "bedrock/us.anthropic.claude-opus-4-8-v1:0"}},
        ],
        card_providers={"claude-opus-4-8": {"bedrock"}},
    )
    out = await check_provider_config(request, db=AsyncMock())

    assert out.unbillable_deployments == []
    assert out.orphan_cards == []


@pytest.mark.asyncio
async def test_unprefixed_catalog_id_resolves_through_the_catalog_and_is_not_flagged():
    """Regression: unprefixed model ids are the NORM for Bedrock (LiteLLM's cost map keys them bare),
    and those deployments bill fine — litellm resolves the id through the same cost map at call time
    and the logger stamps the family on the usage row. Reading only the stored routing params reported
    every one of them as "will bill $0"."""
    request = _config_request(
        gateway_models=[
            {
                "model_name": "claude-haiku-4-5-20251001-v1:0",
                "litellm_params": {"model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0"},
            },
        ],
        card_providers={"claude-haiku-4-5-20251001-v1:0": {"bedrock"}},
        catalog=[{"model_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0", "provider": "bedrock_converse"}],
    )
    out = await check_provider_config(request, db=AsyncMock())

    assert out.unbillable_deployments == []


@pytest.mark.asyncio
async def test_unprefixed_id_the_catalog_does_not_know_is_flagged_without_a_provider():
    """The genuinely unresolvable case: no prefix, no custom_llm_provider, and the catalog has never
    heard of the id. Nothing can bill it AND nothing can route it, so this is not fixable by re-keying
    and the UI must not pretend otherwise."""
    request = _config_request(
        gateway_models=[
            {"model_name": "claude-bare", "litellm_params": {"model": "some-hand-typed-id"}},
        ],
        card_providers={"claude-bare": {"bedrock"}},
        # A readable catalog that simply doesn't list this id — the difference from the outage case.
        catalog=[{"model_id": "amazon.titan-embed-image-v1", "provider": "bedrock"}],
    )
    out = await check_provider_config(request, db=AsyncMock())

    (flagged,) = out.unbillable_deployments
    assert flagged.reason == "provider_underivable"
    assert flagged.runtime_provider is None
    assert flagged.rekey_candidates == []
    assert flagged.other_providers == ["bedrock"]  # the card is named, since it explains the state


@pytest.mark.asyncio
async def test_unreadable_catalog_does_not_flag_unprefixed_deployments():
    """get_catalog fails soft (stale cache, else []), and unprefixed ids are resolved THROUGH it — so a
    catalog outage would otherwise turn every bare Bedrock deployment into a red "will bill $0" row.
    Unresolved-because-unreadable must stay silent; unresolved-because-unroutable is the finding."""
    request = _config_request(
        gateway_models=[
            {"model_name": "claude-bare", "litellm_params": {"model": "eu.anthropic.claude-bare-v1:0"}},
            {"model_name": "claude-prefixed", "litellm_params": {"model": "bedrock/us.anthropic.claude-x-v1:0"}},
        ],
        card_providers={"claude-bare": {"bedrock"}, "claude-prefixed": {"bedrock"}},
        catalog=None,  # → get_catalog() == []
    )
    out = await check_provider_config(request, db=AsyncMock())

    # The bare one is not accused; the prefixed one still resolves on its own and is still checked.
    assert out.unbillable_deployments == []


@pytest.mark.asyncio
async def test_card_pricing_another_deployment_of_the_alias_is_not_a_rekey_candidate():
    """One alias, two deployments: the vertex_ai card is pricing the vertex route, so moving it to fix
    the bedrock route would un-bill live traffic. Name it, never offer it."""
    request = _config_request(
        gateway_models=[
            {"model_name": "claude-dual", "litellm_params": {"model": "bedrock/us.anthropic.claude-dual-v1:0"}},
            {"model_name": "claude-dual", "litellm_params": {"model": "vertex_ai/claude-dual"}},
        ],
        card_providers={"claude-dual": {"vertex_ai"}},
    )
    out = await check_provider_config(request, db=AsyncMock())

    (flagged,) = out.unbillable_deployments
    assert flagged.runtime_provider == "bedrock"
    assert flagged.other_providers == ["vertex_ai"]
    assert flagged.rekey_candidates == []


@pytest.mark.asyncio
async def test_gateway_down_reports_cards_but_no_deployments():
    """Orphan cards need no gateway, so a gateway blip must not blank the whole check — and the
    deployment half must be reported as unverified rather than clean."""
    from console_backend.models.usage import OrphanCard

    def _boom():
        raise RuntimeError("gateway down")

    request = _config_request(
        gateway_models=_boom,
        orphans=[{"provider": "eu", "model_name": "claude-loc", "model_name_pattern": None}],
    )
    out = await check_provider_config(request, db=AsyncMock())

    assert out.gateway_checked is False
    assert out.unbillable_deployments == []
    assert out.orphan_cards == [OrphanCard(provider="eu", model_name="claude-loc")]


# --- backward direction: orphan cards (real DB) ---


def _repo() -> RateCardRepository:
    repo = RateCardRepository()
    repo.set_audit_service(AuditService())
    return repo


async def _create_card(
    repo: RateCardRepository,
    db: AsyncSession,
    actor: User,
    provider: str,
    model: str,
    *,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
    model_name_pattern: str | None = None,
) -> None:
    entry_id = await repo.create_entry(
        db=db, actor=actor, provider=provider, model_name=model,
        model_name_pattern=model_name_pattern,
        billing_unit="base_input_tokens", flow_direction="input",
        price_per_million=Decimal("5.50"), effective_from=effective_from or T0,
    )
    if effective_until is not None:
        await db.execute(
            text("UPDATE rate_card_entries SET effective_until = :until WHERE id = :id"),
            {"until": effective_until, "id": entry_id},
        )
        await db.flush()


@pytest.mark.asyncio
async def test_orphan_card_is_found_without_any_usage(pg_session: AsyncSession, test_user: User):
    """What migration 076 had to clean up by hand: a card under a vocabulary the runtime never emits.
    No traffic is needed to know it can never match."""
    repo = _repo()
    service = RateCardService(repo)
    await _create_card(repo, pg_session, test_user, "bedrock_converse", "claude-orphan")
    await _create_card(repo, pg_session, test_user, "bedrock", "claude-fine")

    orphans = await service.find_orphan_cards(pg_session)

    keys = {(o["provider"], o["model_name"]) for o in orphans}
    assert ("bedrock_converse", "claude-orphan") in keys
    assert not any(provider == "bedrock" for provider, _ in keys)


@pytest.mark.asyncio
async def test_expired_orphan_card_is_not_reported(pg_session: AsyncSession, test_user: User):
    """A card whose pricing has lapsed is not billing anything wrong — only live pricing is a finding,
    or the banner would never clear for historical keys."""
    repo = _repo()
    service = RateCardService(repo)
    await _create_card(
        repo, pg_session, test_user, "bedrock_converse", "claude-lapsed-orphan",
        effective_until=datetime.now(timezone.utc) - timedelta(days=1),
    )

    orphans = await service.find_orphan_cards(pg_session)
    assert [o for o in orphans if o["model_name"] == "claude-lapsed-orphan"] == []


# --- find_card_providers_for_models: "priced" must mean what billing means (real DB) ---


@pytest.mark.asyncio
async def test_pattern_card_counts_as_pricing_the_alias(pg_session: AsyncSession, test_user: User):
    """A pattern card is how families are priced in this product. Reading only exact names here is
    what made the check report a covered model as uncovered (and hide a movable card)."""
    repo = _repo()
    await _create_card(
        repo, pg_session, test_user, "bedrock", "claude-fam",
        model_name_pattern=r"^claude-fam(-v\d+)?$",
    )

    providers = await repo.find_card_providers_for_models(pg_session, ["claude-fam-v2", "claude-other"])

    # Priced by bedrock, and flagged NOT movable: the card is keyed on another name, so a re-key
    # would 404. The flag is what stops the banner offering it.
    assert providers["claude-fam-v2"] == {"bedrock": False}
    assert "claude-other" not in providers

    # Even for the card's OWN name, a pattern card is not offered: `rekey` would find it, but moving
    # it takes the pricing of every model the regex covers with it. That has to be a deliberate edit.
    own = await repo.find_card_providers_for_models(pg_session, ["claude-fam"])
    assert own["claude-fam"] == {"bedrock": False}


@pytest.mark.asyncio
async def test_scheduled_and_future_entries_are_read_like_billing_reads_them(
    pg_session: AsyncSession, test_user: User
):
    """A closed-ended entry (price change scheduled ahead) is what bills today; an entry that hasn't
    started yet is not. `effective_until IS NULL` gets both backwards."""
    repo = _repo()
    await _create_card(
        repo, pg_session, test_user, "bedrock", "claude-scheduled",
        effective_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    await _create_card(
        repo, pg_session, test_user, "bedrock", "claude-future",
        effective_from=datetime.now(timezone.utc) + timedelta(days=7),
    )

    providers = await repo.find_card_providers_for_models(pg_session, ["claude-scheduled", "claude-future"])

    assert providers["claude-scheduled"] == {"bedrock": True}
    assert "claude-future" not in providers
    # …and the billing lookup agrees, which is the whole point of sharing the predicate.
    assert await repo.get_all_active_rates(pg_session, "bedrock", "claude-scheduled")
    assert await repo.get_all_active_rates(pg_session, "bedrock", "claude-future") == {}


@pytest.mark.asyncio
async def test_check_reflects_a_fix_immediately(pg_session: AsyncSession, test_user: User):
    """No result cache on this half: the banner refetch after a re-key must see the fixed state, and
    there is no TTL to defeat (the usage audit is the one that caches)."""
    repo = _repo()
    service = RateCardService(repo)
    await _create_card(repo, pg_session, test_user, "bedrock_converse", "claude-fixme")
    gateway = SimpleNamespace(
        list_models=AsyncMock(
            return_value=[
                {"model_name": "claude-fixme", "litellm_params": {"model": "bedrock/us.anthropic.claude-fixme-v1:0"}}
            ]
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(model_gateway_service=gateway, rate_card_service=service))
    )

    before = await check_provider_config(request, pg_session)
    assert [d.rekey_candidates for d in before.unbillable_deployments] == [["bedrock_converse"]]

    await service.rekey_model_provider(
        db=pg_session, actor=test_user, model_name="claude-fixme",
        from_provider="bedrock_converse", to_provider="bedrock",
    )

    after = await check_provider_config(request, pg_session)
    assert after.unbillable_deployments == []
    assert [o for o in after.orphan_cards if o.model_name == "claude-fixme"] == []
