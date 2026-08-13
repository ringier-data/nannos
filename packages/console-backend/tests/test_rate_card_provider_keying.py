"""Rate-card provider keying: derivation, mismatch detection, and re-key.

Rate cards must be keyed on the provider the cost logger reports at runtime
(custom_llm_provider, else the model-id route prefix — see AGENTS.md "Rate-Card Provider
Must Equal the litellm Provider Family"), NOT on LiteLLM's cost-map implementation tag
(`bedrock_converse`, `vertex_ai-language-models`). A card keyed to the wrong vocabulary
never matches usage and the model silently bills $0. These tests cover:
- runtime_billing_provider (the shared derivation rule),
- register_model deriving the rate-card key server-side (and rejecting underivable params),
- assert_billable_provider on the Rate Cards page's own (admin-typed) write paths,
- rekey_model_provider (the one-click fix; history moves, conflicts refused).

The surface that FINDS mis-keying lives in test_provider_config_check.py (configuration, before any
traffic).
"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from console_backend.models.model_gateway import ModelRegistrationRequest
from console_backend.models.usage import RateCardPricingEntry
from console_backend.models.user import User
from console_backend.repositories.rate_card_repository import RateCardRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.rate_card_service import RateCardService, runtime_billing_provider
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


# --- runtime_billing_provider: the shared derivation rule ---


def test_explicit_custom_llm_provider_wins():
    params = {"custom_llm_provider": "vertex_ai", "model": "bedrock/whatever"}
    assert runtime_billing_provider(params) == "vertex_ai"


def test_provider_derived_from_model_prefix():
    assert runtime_billing_provider({"model": "bedrock/us.anthropic.claude-opus-4-8-v1:0"}) == "bedrock"
    assert runtime_billing_provider({"model": "vertex_ai/gemini-2.5-pro"}) == "vertex_ai"


def test_unprefixed_model_is_underivable():
    # An unprefixed cost-map id (catalog tag bedrock_converse) must NOT guess — the runtime
    # family is litellm-internal knowledge; callers reject instead of mis-keying billing.
    assert runtime_billing_provider({"model": "us.anthropic.claude-opus-4-8-v1:0"}) is None
    assert runtime_billing_provider({}) is None


# --- register_model: server-side keying ---


def _register_request(
    created_ids: list[int],
    registered: list[dict] | None = None,
    catalog: list[dict] | None = None,
):
    """A request whose rate-card service records create_model_rate_card kwargs and whose gateway
    accepts the registration. ``registered`` seeds the aliases already on the gateway; ``catalog``
    the cost-map entries the server resolves unprefixed model ids against."""
    rate_card_service = SimpleNamespace(
        create_model_rate_card=AsyncMock(return_value=created_ids),
    )
    entries = catalog if catalog is not None else _CATALOG
    gateway = SimpleNamespace(
        register_model=AsyncMock(return_value={"model_info": {"id": "gw-1"}}),
        list_models=AsyncMock(return_value=registered or []),
        catalog_model=AsyncMock(side_effect=lambda mid: next((c for c in entries if c["model_id"] == mid), None)),
        # Readability is what separates "unknown model id" (422) from "catalog outage" (502).
        get_catalog=AsyncMock(return_value=entries),
    )
    state = SimpleNamespace(model_gateway_service=gateway, rate_card_service=rate_card_service)
    return SimpleNamespace(app=SimpleNamespace(state=state)), rate_card_service, gateway


# Cost-map entries as get_catalog() normalizes them: bare ids (the norm for Bedrock) carrying
# LiteLLM's implementation tag, which is what the server — not the client — turns into a route.
_CATALOG = [
    {"model_id": "eu.anthropic.claude-opus-4-8", "provider": "bedrock_converse"},
    {"model_id": "us.anthropic.claude-opus-4-8-v1:0", "provider": "bedrock_converse"},
    {"model_id": "eu.amazon.nova-2-lite-v1:0", "provider": "bedrock_converse"},
    {"model_id": "gemini-2.5-pro", "provider": "vertex_ai-language-models"},
]


def _body(litellm_params: dict) -> ModelRegistrationRequest:
    return ModelRegistrationRequest(
        model_name="claude-opus-4-8",
        litellm_params=litellm_params,
        pricing={"base_input_tokens": RateCardPricingEntry(price_per_million=Decimal("5.50"), flow_direction="input")},
    )


@pytest.mark.asyncio
async def test_register_keys_rate_card_on_the_model_id_route():
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, gateway = _register_request([1])
    db = AsyncMock()
    body = _body({"model": "bedrock/us.anthropic.claude-opus-4-8-v1:0"})

    await router.register_model(request, body, db, user=SimpleNamespace(id="admin"))

    kwargs = rate_card_service.create_model_rate_card.await_args.kwargs
    assert kwargs["provider"] == "bedrock"  # the route prefix on the deployment id, nothing else
    gateway.register_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_auto_prefixes_unprefixed_catalog_id():
    """Unprefixed cost-map ids are the norm (every Bedrock entry). The server looks the id up in its
    OWN catalog, normalizes that entry's tag to a route family and prefixes the model id — so the
    deployment id, the runtime provider and the rate-card key agree by construction, with the client
    sending no provider value at all."""
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, gateway = _register_request([1])
    body = _body({"model": "eu.anthropic.claude-opus-4-8"})

    await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert rate_card_service.create_model_rate_card.await_args.kwargs["provider"] == "bedrock"
    registered_params = gateway.register_model.await_args.args[1]
    assert registered_params["model"] == "bedrock/eu.anthropic.claude-opus-4-8"


@pytest.mark.asyncio
async def test_register_reports_the_key_it_actually_used():
    """The response carries the resolved provider — the only way a client learns the key, since it
    sends no provider value of its own."""
    import console_backend.routers.admin_model_gateway_router as router

    request, _, _ = _register_request([1])
    body = _body({"model": "eu.anthropic.claude-opus-4-8"})

    result = await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert result.provider == "bedrock"  # resolved from the catalog tag, server-side


@pytest.mark.asyncio
async def test_register_auto_prefixes_vertex_tags():
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, gateway = _register_request([1])
    body = _body({"model": "gemini-2.5-pro"})

    await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert rate_card_service.create_model_rate_card.await_args.kwargs["provider"] == "vertex_ai"
    registered_params = gateway.register_model.await_args.args[1]
    assert registered_params["model"] == "vertex_ai/gemini-2.5-pro"


@pytest.mark.asyncio
async def test_register_rejects_an_unprefixed_id_the_catalog_does_not_know():
    """Nothing to derive from: no route prefix, no custom_llm_provider, and the id isn't a catalog
    model for an integrated provider (a hand-typed id, a custom endpoint). Reject and ask for a
    prefixed id — never guess a route, since it decides what bills."""
    import console_backend.routers.admin_model_gateway_router as router

    # A READABLE catalog that just doesn't list this id — see the outage test below for the other case.
    request, rate_card_service, gateway = _register_request([1])
    body = _body({"model": "my-custom-deployment"})

    with pytest.raises(HTTPException) as exc:
        await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert exc.value.status_code == 422
    assert "prefix" in exc.value.detail.lower()
    rate_card_service.create_model_rate_card.assert_not_awaited()  # nothing written
    gateway.register_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_catalog_is_a_502_not_a_bad_model_id():
    """get_catalog degrades to [] when the cost map can't be read, so an unprefixed-but-valid id would
    otherwise be rejected as "not a known catalog model" — sending the admin to debug an id that was
    never the problem. An outage must say so."""
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, gateway = _register_request([1], catalog=[])
    body = _body({"model": "eu.anthropic.claude-opus-4-8"})  # a real catalog id, unreadable right now

    with pytest.raises(HTTPException) as exc:
        await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert exc.value.status_code == 502
    assert "outage" in exc.value.detail.lower() or "unreadable" in exc.value.detail.lower()
    rate_card_service.create_model_rate_card.assert_not_awaited()
    gateway.register_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_accepts_a_routable_vendor_prefix_outside_the_family_allowlist():
    """A prefix the admin wrote on the model id IS the route litellm uses and the value the cost logger
    stamps, so it is billable by construction. Applying the family allowlist here used to 422
    `anthropic/…` with "not a runtime billing provider" right after the sibling error asked for that
    very prefix."""
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, gateway = _register_request([1])
    body = _body({"model": "anthropic/claude-opus-4-8"})

    await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert rate_card_service.create_model_rate_card.await_args.kwargs["provider"] == "anthropic"
    gateway.register_model.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["bedrock_converse", "vertex_ai-anthropic_models"])
async def test_derived_writes_still_refuse_a_catalog_tag(tag: str):
    """The relaxation is only for values the runtime can actually route. A cost-map tag is not one
    (`bedrock_converse/…` isn't a routable prefix, and get_llm_provider normalizes the tag away), so a
    card under it would never match usage — refused on the derived path too."""
    service, repo = _service_with_mock_repo()
    pricing = {"base_input_tokens": RateCardPricingEntry(price_per_million=Decimal("1"), flow_direction="input")}

    with pytest.raises(ValueError, match="cost-map tag"):
        await service.create_model_rate_card(
            db=AsyncMock(), actor=SimpleNamespace(sub="admin"), provider=tag, model_name="m",
            pricing=pricing, derived_from_deployment=True,
        )
    repo.create_model_rate_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_derived_writes_accept_a_vendor_outside_the_family_allowlist():
    """The counterpart: `anthropic` isn't in runtime_provider_families() for this deployment, but a
    deployment routed as `anthropic/…` bills under exactly that key."""
    service, repo = _service_with_mock_repo()
    pricing = {"base_input_tokens": RateCardPricingEntry(price_per_million=Decimal("1"), flow_direction="input")}

    await service.create_model_rate_card(
        db=AsyncMock(), actor=SimpleNamespace(sub="admin"), provider="anthropic", model_name="m",
        pricing=pricing, derived_from_deployment=True,
    )

    assert repo.create_model_rate_card.await_args.kwargs["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_register_rejects_a_catalog_tag_with_no_route_family():
    """A catalog entry whose tag maps to no known family (litellm drift, or a tag forced in via
    LLM_GATEWAY_PROVIDERS that isn't a route) resolves to nothing — same refusal, no silent guess."""
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, _ = _register_request(
        [1], catalog=[{"model_id": "weird-model", "provider": "some_new_tag"}]
    )

    with pytest.raises(HTTPException) as exc:
        await router.register_model(request, _body({"model": "weird-model"}), AsyncMock(), user=SimpleNamespace(id="admin"))

    assert exc.value.status_code == 422
    rate_card_service.create_model_rate_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_annotates_each_entry_with_its_route_family():
    """The picker gets the resolved route as data, so no client re-implements the tag→family
    normalization (and can show `bedrock` rather than the `bedrock_converse` tag)."""
    import console_backend.routers.admin_model_gateway_router as router

    gateway = SimpleNamespace(get_catalog=AsyncMock(return_value=list(_CATALOG)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(model_gateway_service=gateway)))

    out = await router.model_catalog(request, user=SimpleNamespace(id="admin"))

    by_id = {e["model_id"]: e["family"] for e in out}
    assert by_id["eu.amazon.nova-2-lite-v1:0"] == "bedrock"
    assert by_id["gemini-2.5-pro"] == "vertex_ai"


@pytest.mark.asyncio
async def test_register_accepts_explicit_custom_llm_provider():
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, _ = _register_request([1])
    body = _body({"model": "gemini-2.5-pro", "custom_llm_provider": "vertex_ai"})

    await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert rate_card_service.create_model_rate_card.await_args.kwargs["provider"] == "vertex_ai"


@pytest.mark.asyncio
async def test_register_refuses_an_alias_that_is_already_registered():
    """An alias addresses exactly one deployment: the rate card, the role defaults and the provider
    check are all keyed on it, and Edit/Remove act on a single gateway id. A second deployment under
    the same name would silently load-balance a model the admin can only manage half of."""
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, gateway = _register_request(
        [1], registered=[{"model_name": "claude-opus-4-8", "litellm_params": {"model": "bedrock/x"}}]
    )
    body = _body({"model": "bedrock/us.anthropic.claude-opus-4-8-v1:0"})

    with pytest.raises(HTTPException) as exc:
        await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert exc.value.status_code == 409
    # Refused before the rate-card write, so a rejected registration leaves nothing behind.
    rate_card_service.create_model_rate_card.assert_not_awaited()
    gateway.register_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_proceeds_when_the_alias_list_is_unavailable():
    """Fails open: an unlistable gateway must not block registration — the register call itself
    surfaces the outage as a 502."""
    import console_backend.routers.admin_model_gateway_router as router
    from console_backend.services.model_gateway_service import ModelGatewayError

    request, rate_card_service, gateway = _register_request([1])
    gateway.list_models = AsyncMock(side_effect=ModelGatewayError("gateway down"))
    body = _body({"model": "bedrock/us.anthropic.claude-opus-4-8-v1:0"})

    await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    gateway.register_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_editing_a_model_keeps_its_own_alias():
    """The duplicate guard is registration-only: an edit re-registers the SAME alias by design."""
    import console_backend.routers.admin_model_gateway_router as router

    request, rate_card_service, gateway = _register_request(
        [1], registered=[{"model_name": "claude-opus-4-8", "litellm_params": {"model": "bedrock/x"}}]
    )
    gateway.update_model = AsyncMock(return_value={"model_info": {"id": "gw-2"}})
    body = _body({"model": "bedrock/us.anthropic.claude-opus-4-8-v1:0"})

    result = await router.edit_model("gw-1", request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

    assert result.status == "updated"
    gateway.update_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_accepts_a_provider_added_via_env():
    """LLM_GATEWAY_PROVIDERS is the supported way to integrate another vendor. Its catalog entries
    come with unprefixed ids, so the family vocabulary must include it — otherwise registration 422s
    on a deployment that is configured correctly."""
    import console_backend.routers.admin_model_gateway_router as router
    from console_backend.config import config

    original = config.model_gateway.integrated_providers
    config.model_gateway.integrated_providers = [*original, "mistral"]
    try:
        # Its catalog entries are tagged with the vendor name, which for such providers IS the route.
        request, rate_card_service, gateway = _register_request(
            [1], catalog=[{"model_id": "mistral-large-latest", "provider": "mistral"}]
        )
        body = _body({"model": "mistral-large-latest"})

        await router.register_model(request, body, AsyncMock(), user=SimpleNamespace(id="admin"))

        assert rate_card_service.create_model_rate_card.await_args.kwargs["provider"] == "mistral"
        assert gateway.register_model.await_args.args[1]["model"] == "mistral/mistral-large-latest"
    finally:
        config.model_gateway.integrated_providers = original


# --- The same invariant on the Rate Cards page's own write paths ---
#
# register/edit DERIVE the provider from the deployment's routing params; the Rate Cards page passes
# an admin-typed value. Both converge on RateCardService, which is where the invariant is enforced —
# otherwise a card mis-keyed by hand bills $0 exactly like the ones migration 076 cleans up.


def _service_with_mock_repo():
    repo = SimpleNamespace(
        create_entry=AsyncMock(return_value=1),
        create_model_rate_card=AsyncMock(return_value=[1]),
        copy_model_rates=AsyncMock(return_value=[1]),
    )
    return RateCardService(repo), repo


@pytest.mark.parametrize("provider", ["bedrock_converse", "eu", "bedrock-anthropic", "anthropic"])
@pytest.mark.asyncio
async def test_manual_rate_card_writes_reject_non_runtime_providers(provider: str):
    service, repo = _service_with_mock_repo()
    actor = SimpleNamespace(sub="admin")
    pricing = {"base_input_tokens": RateCardPricingEntry(price_per_million=Decimal("1"), flow_direction="input")}

    with pytest.raises(ValueError, match="not a runtime billing provider"):
        await service.create_entry(
            db=AsyncMock(), actor=actor, provider=provider, model_name="m",
            billing_unit="base_input_tokens", flow_direction="input", price_per_million=Decimal("1"),
        )
    with pytest.raises(ValueError, match="not a runtime billing provider"):
        await service.create_model_rate_card(
            db=AsyncMock(), actor=actor, provider=provider, model_name="m", pricing=pricing
        )
    with pytest.raises(ValueError, match="not a runtime billing provider"):
        await service.copy_model_rates(
            db=AsyncMock(), actor=actor, source_provider="bedrock", source_model="src",
            target_provider=provider, target_model="m",
        )
    repo.create_entry.assert_not_awaited()  # rejected before anything is written
    repo.create_model_rate_card.assert_not_awaited()
    repo.copy_model_rates.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_rate_card_write_accepts_a_runtime_family():
    service, repo = _service_with_mock_repo()
    await service.create_entry(
        db=AsyncMock(), actor=SimpleNamespace(sub="admin"), provider="bedrock", model_name="m",
        billing_unit="base_input_tokens", flow_direction="input", price_per_million=Decimal("1"),
    )
    assert repo.create_entry.await_args.kwargs["provider"] == "bedrock"


@pytest.mark.asyncio
async def test_rate_cards_endpoint_answers_422_on_a_non_billable_provider():
    """The page's free-text provider field must fail loudly, not create a $0-billing card."""
    import console_backend.routers.rate_card_router as rc_router
    from console_backend.models.usage import RateCardEntryCreate

    service, repo = _service_with_mock_repo()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rate_card_service=service)))
    entry = RateCardEntryCreate(
        provider="bedrock_converse", model_name="claude-opus-4-8", billing_unit="base_input_tokens",
        flow_direction="input", price_per_million=Decimal("5.50"),
    )

    with pytest.raises(HTTPException) as exc:
        await rc_router.create_rate_card_entry(request, entry, AsyncMock(), current_user=SimpleNamespace(sub="admin"))

    assert exc.value.status_code == 422
    repo.create_entry.assert_not_awaited()


# --- rekey_model_provider: the one-click fix both surfaces offer (real DB) ---


def _repo() -> RateCardRepository:
    repo = RateCardRepository()
    repo.set_audit_service(AuditService())
    return repo


async def _create_card(repo: RateCardRepository, db: AsyncSession, actor: User, provider: str, model: str) -> None:
    await repo.create_entry(
        db=db, actor=actor, provider=provider, model_name=model,
        billing_unit="base_input_tokens", flow_direction="input",
        price_per_million=Decimal("5.50"), effective_from=T0,
    )


@pytest.mark.asyncio
async def test_rekey_refuses_missing_source_and_existing_target(
    pg_session: AsyncSession, test_user: User
):
    repo = _repo()
    model = "claude-conflict"
    await _create_card(repo, pg_session, test_user, "bedrock_converse", model)
    await _create_card(repo, pg_session, test_user, "bedrock", model)

    with pytest.raises(LookupError):
        await repo.rekey_model_provider(
            db=pg_session, actor=test_user, model_name=model,
            from_provider="nope", to_provider="bedrock",
        )
    with pytest.raises(ValueError):
        await repo.rekey_model_provider(
            db=pg_session, actor=test_user, model_name=model,
            from_provider="bedrock_converse", to_provider="bedrock",
        )


@pytest.mark.asyncio
async def test_rekey_translates_a_losing_race_into_a_conflict():
    """The target-key check is check-then-act: a concurrent create/re-key can claim that key before
    this UPDATE lands, and uq_rate_card then fires. It must surface as the documented 409 (ValueError)
    instead of an unhandled IntegrityError — a 500 with a raw DB error — so two admins clicking the
    same banner button get the merge-manually message."""
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import IntegrityError

    class _Result:
        def __init__(self, row):
            self._row = row

        def mappings(self):
            return SimpleNamespace(first=lambda: self._row)

    class _RacingDb:
        """Source card exists, target key looks free — then the UPDATE loses the race."""

        async def execute(self, stmt, params=None):
            if str(stmt).strip().startswith("UPDATE"):
                raise IntegrityError(
                    "UPDATE rate_cards", {}, Exception('duplicate key value violates unique constraint "uq_rate_card"')
                )
            return _Result({"id": 7} if params["provider"] == "bedrock_converse" else None)

        def begin_nested(self):
            @asynccontextmanager
            async def _savepoint():
                yield None

            return _savepoint()

    repo = _repo()
    with pytest.raises(ValueError, match="already exists"):
        await repo.rekey_model_provider(
            db=_RacingDb(), actor=SimpleNamespace(sub="admin"), model_name="claude-raced",
            from_provider="bedrock_converse", to_provider="bedrock",
        )


@pytest.mark.asyncio
async def test_rekey_is_audited(pg_session: AsyncSession, test_user: User):
    """Re-keying moves which provider a model bills under — an admin write, so it must leave an
    audit trail with both sides of the change (AGENTS.md: all write operations generate audit logs
    and tests verify it)."""
    repo = _repo()
    model = "claude-audited"
    await _create_card(repo, pg_session, test_user, "bedrock_converse", model)

    rate_card_id = await repo.rekey_model_provider(
        db=pg_session, actor=test_user, model_name=model,
        from_provider="bedrock_converse", to_provider="bedrock",
    )

    # The card's own creation is audited too, and both rows share the transaction timestamp — select
    # the re-key's action explicitly rather than "the newest row".
    rows = (
        await pg_session.execute(
            text(
                "SELECT changes FROM audit_logs "
                "WHERE entity_type = 'rate_card' AND entity_id = :entity_id AND action = 'update'"
            ),
            {"entity_id": str(rate_card_id)},
        )
    ).mappings().all()
    assert len(rows) == 1
    changes = rows[0]["changes"]
    assert changes["before"] == {"provider": "bedrock_converse", "model_name": model}
    assert changes["after"] == {"provider": "bedrock", "model_name": model}
