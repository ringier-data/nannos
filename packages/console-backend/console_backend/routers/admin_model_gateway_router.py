"""Admin router for runtime model registration via the Model Gateway.

console-backend is the single front door for adding/editing models: it writes the
billing Rate Card (authoritative billed rate) and registers routing+capability on
the LiteLLM proxy. The Rate Card is written FIRST so a model is
never usable before it is billable. Master-key access stays server-side.
"""

import logging
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..config import config
from ..db.session import DbSession
from ..dependencies import require_admin
from ..models.model_gateway import (
    BedrockModelRegions,
    CatalogModel,
    CostPrefill,
    GatewayModel,
    GatewayUiConfig,
    ModelRegistrationRequest,
    ModelRegistrationResponse,
    SetDefaultRequest,
    WebSearchConfig,
)
from ..models.usage import RateCardPricingEntry
from ..models.user import User
from ..services.bedrock_availability_service import model_regions, probed_regions
from ..services.model_defaults_service import ModelDefaultsService
from ..services.model_gateway_service import ModelGatewayError, ModelGatewayService
from ..services.rate_card_service import resolve_deployment_provider, route_family
from ..services.rate_card_service import runtime_billing_provider as _billing_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/model-gateway", tags=["admin-model-gateway"])

# cost-per-token (gateway) → price-per-million (rate card), with the billing-unit
# names the proxy CustomLogger emits.
_FlowDir = Literal["input", "output", "other"]
_COST_FIELD_TO_UNIT: dict[str, tuple[str, _FlowDir]] = {
    "input_cost_per_token": ("base_input_tokens", "input"),
    "output_cost_per_token": ("base_output_tokens", "output"),
    "cache_read_input_token_cost": ("cache_read_input_tokens", "input"),
    "cache_creation_input_token_cost": ("cache_creation_input_tokens", "input"),
    "input_cost_per_image": ("input_images", "input"),
}

# billing_unit → flow_direction, for rate-card rows (which store only unit + price). Unknown
# units default to "input" (the common case; only base_output_tokens is output-side).
_UNIT_TO_FLOW = {unit: flow for (unit, flow) in _COST_FIELD_TO_UNIT.values()}
# Web search isn't a per-token cost field (model_info carries it as a per-query dict, seeded
# separately below), so it has no _COST_FIELD_TO_UNIT entry — register its flow explicitly so the
# stored-rate edit path groups it the same way the registration seed does.
_UNIT_TO_FLOW["web_search"] = "output"


async def _resolve_billing_provider(
    svc: ModelGatewayService, body: ModelRegistrationRequest
) -> tuple[str | None, dict]:
    """Billing provider + (possibly auto-prefixed) litellm_params for a register/edit body.

    ONE value decides routing, provider-specific rules and billing: the provider route on the
    deployment. A prefixed model id or an explicit custom_llm_provider states it outright (the same
    rule the cost logger applies). Otherwise — the norm for Bedrock, whose cost-map ids are bare —
    it is resolved from the SERVER's own catalog: exact id lookup → its LiteLLM tag →
    ``route_family`` → prefix onto the model id. The client sends nothing about providers, so no
    admin-typed value can participate in keying billing. (None, params) when nothing resolves;
    callers 422 rather than guess.
    """
    litellm_params = body.litellm_params
    provider = _billing_provider(litellm_params)
    if provider:
        return provider, litellm_params
    # Same resolution the provider-config check applies to already-registered deployments
    # (resolve_deployment_provider); registration additionally PINS it onto the model id, so the
    # deployment states its own route from then on instead of relying on the catalog every time.
    family = await resolve_deployment_provider(svc, litellm_params)
    if not family:
        return None, litellm_params
    return family, {**litellm_params, "model": f"{family}/{litellm_params.get('model')}"}


async def _catalog_is_readable(svc: ModelGatewayService) -> bool:
    """Whether the model catalog can be read at all (it degrades to [] on failure, never raising).

    Used only to tell "we looked and this id isn't a known model" apart from "we couldn't look".
    """
    try:
        return bool(await svc.get_catalog())
    except Exception as e:  # get_catalog is already fail-soft; never let this decide a 500
        logger.warning("Catalog readability check failed: %s", e)
        return False


def _with_default_vertex_location(litellm_params: dict, provider: str) -> dict:
    """Pin the deployment's default Vertex serving region when a Vertex model omits one.

    LiteLLM resolves an unpinned vertex_location for DB-registered models to its own default
    (us-central1) — NOT the proxy's DEFAULT_VERTEXAI_LOCATION — so a blank location silently routes
    to the wrong region and 404s models served elsewhere (e.g. EU-only Gemini embeddings). Pinning
    config.model_gateway.default_vertex_location (env DEFAULT_VERTEXAI_LOCATION) keeps the UI's
    "leave blank → deployment default" promise true. A region, not a credential — safe to inject.
    """
    model = str(litellm_params.get("model") or "")
    is_vertex = provider.startswith("vertex_ai") or model.startswith("vertex_ai/")
    if is_vertex and not litellm_params.get("vertex_location"):
        return {**litellm_params, "vertex_location": config.model_gateway.default_vertex_location}
    return litellm_params


async def _write_rate_card_and_routing(
    request: Request,
    body: ModelRegistrationRequest,
    db: DbSession,
    user: User,
) -> tuple[list[int], str, dict, dict]:
    """Rate card first, then the resolved gateway payload — the shared half of register and edit.

    Returns ``(rate_card_entry_ids, provider, litellm_params, model_info)``. The card is keyed on the
    provider the cost logger will report at runtime — resolved server-side by
    ``_resolve_billing_provider`` — and the write is committed before the gateway is touched, so a
    model is never usable before it is billable. Unresolvable or non-billable providers 422:
    registering anyway would create a card that never matches usage and the model would bill $0.

    ``input_modes`` + ``mode`` are always written into model_info: input_modes so every model
    declares its accepted payloads (orchestrator/sub-agents depend on it), mode so chat vs embedding
    is explicit (the chat picker filters on mode=chat).
    """
    svc = get_model_gateway_service(request)
    provider, litellm_params = await _resolve_billing_provider(svc, body)
    if not provider:
        # "Unprefixed and unknown" has two very different causes, and get_catalog fails SOFT (stale
        # cache, else []) — so an unreadable catalog would otherwise be reported as a bad model id and
        # send the admin off to debug an id that was fine. Separate them: an outage is a 502.
        if not await _catalog_is_readable(svc):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Cannot resolve the provider route for '{body.litellm_params.get('model')}': the "
                    "model catalog is currently unreadable, so an unprefixed id can't be looked up. "
                    "This is a gateway/catalog outage, not a bad model id — retry, or prefix the id "
                    "with its route (e.g. 'bedrock/…') to register without the catalog."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Cannot resolve the provider route for '{body.litellm_params.get('model')}': it "
                "carries no provider prefix and is not a known catalog model for an integrated "
                "provider. Prefix the gateway model id with its route (e.g. "
                "'bedrock/eu.amazon.nova-2-lite-v1:0', 'vertex_ai/gemini-2.5-pro') or set "
                "litellm_params.custom_llm_provider. That route is what the gateway bills under, so "
                "it can't be guessed."
            ),
        )

    try:
        entry_ids = await request.app.state.rate_card_service.create_model_rate_card(
            db=db,
            actor=user,
            provider=provider,
            model_name=body.model_name,
            pricing=body.pricing,
            model_name_pattern=body.model_name_pattern,
            # The route came from the deployment itself, so it is what the cost logger will stamp —
            # any routable vendor is billable here, not just the ones on the family allowlist.
            derived_from_deployment=True,
        )
    except ValueError as e:
        # Non-billable provider key (a catalog tag or a location prefixed onto the model id), or
        # incomplete pricing — either way nothing usable was written; don't leave a partial card.
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    await db.commit()

    model_info = {**body.model_info, "input_modes": body.input_modes, "mode": body.mode}
    return entry_ids, provider, _with_default_vertex_location(litellm_params, provider), model_info


def _gateway_model_id(result: object) -> str | None:
    """The gateway deployment id from a /model/new (register or re-register) response, or None.

    Both the register and edit endpoints read it the same way; the result is the raw gateway
    response, so tolerate a non-dict / missing model_info defensively.
    """
    return (result.get("model_info") or {}).get("id") if isinstance(result, dict) else None


# NOTE: Registrations carry NO per-model provider credentials. The proxy is the auth
# authority for every provider: Vertex via pod ADC (GOOGLE_APPLICATION_CREDENTIALS, a file
# projected from the GCP_KEY secret), Bedrock via the pod IAM role, Azure via the proxy's
# AZURE_OPENAI_API_KEY env. In particular, do NOT inject vertex_credentials="os.environ/GCP_KEY":
# DB-registered (runtime) models do not resolve os.environ/* refs (the proxy config is
# settings-only, no model_list), so the literal string reaches json.loads() and fails with
# "Unable to load vertex credentials ... JSONDecodeError". (Earlier code did this to work around
# ADC not being wired; ADC is wired now — see gitops litellm-proxy.yaml.)


def get_model_gateway_service(request: Request) -> ModelGatewayService:
    return request.app.state.model_gateway_service


def get_model_defaults_service(request: Request) -> ModelDefaultsService:
    return request.app.state.model_defaults_service


@router.get("/models", response_model=list[GatewayModel])
async def list_models(request: Request, db: DbSession, user: User = Depends(require_admin)):
    """List models registered on the gateway, annotated with their default role (if any)."""
    try:
        raw = await get_model_gateway_service(request).list_models()
    except ModelGatewayError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    # Defaults live in our DB (not the gateway). One alias may be default for several
    # roles (e.g. both embedding + multimodal_embedding), so map alias → [roles].
    alias_to_roles: dict[str, list[str]] = {}
    for role, alias in (await get_model_defaults_service(request).get_all(db)).items():
        alias_to_roles.setdefault(alias, []).append(role)
    out = []
    for m in raw:
        info = m.get("model_info") or {}
        params = m.get("litellm_params") or {}
        name = m.get("model_name", "")
        out.append(
            GatewayModel(
                model_name=name,
                model_id=info.get("id"),
                # Billing provider first (what the cost logger emits and rate cards key on) so
                # the UI shows/seeds the value that actually bills; the gateway's cost-map tag
                # only for deployments where none can be derived.
                provider=_billing_provider(params) or info.get("litellm_provider"),
                litellm_model=params.get("model"),
                mode=info.get("mode"),
                input_modes=info.get("input_modes") or [],
                default_roles=alias_to_roles.get(name, []),
                db_model=bool(info.get("db_model")),
                base_model=info.get("base_model"),
                vertex_location=params.get("vertex_location"),
                vertex_project=params.get("vertex_project"),
                aws_region_name=params.get("aws_region_name"),
                input_cost_per_token=info.get("input_cost_per_token"),
                output_cost_per_token=info.get("output_cost_per_token"),
                supports_reasoning=info.get("supports_reasoning"),
                supports_vision=info.get("supports_vision"),
                supports_web_search=info.get("supports_web_search"),
            )
        )
    return out


@router.get("/web-search", response_model=WebSearchConfig)
async def web_search_config(request: Request, db: DbSession, user: User = Depends(require_admin)):
    """Fully-resolved Web Search picker state — which web-search-capable models exist (cheapest
    first), which one backs ``console_web_search`` right now, and whether it's the admin's
    ``search`` default or auto-selected. The console renders this verbatim instead of re-deriving
    the pick, so the picker can't disagree with the tool (shared services.web_search resolver)."""
    from ..services.web_search import resolve_web_search_config

    try:
        raw = await get_model_gateway_service(request).list_models()
    except ModelGatewayError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    search_default = (await get_model_defaults_service(request).get_all(db)).get("search")
    return resolve_web_search_config(raw, search_default)


@router.get("/config", response_model=GatewayUiConfig)
async def gateway_ui_config(user: User = Depends(require_admin)):
    """Deployment defaults the registration form needs (env-driven). Keeps the UI's suggested
    Vertex region in sync with the proxy's DEFAULT_VERTEXAI_LOCATION instead of hardcoding it."""
    return GatewayUiConfig(
        default_vertex_location=config.model_gateway.default_vertex_location,
        default_vertex_project=config.model_gateway.default_vertex_project,
        default_bedrock_region=config.model_gateway.default_bedrock_region,
    )


@router.get("/catalog", response_model=list[CatalogModel])
async def model_catalog(request: Request, user: User = Depends(require_admin)):
    """LiteLLM's known-model catalog for the registration picker, pre-filtered to the
    providers this deployment has integrated (config.model_gateway.integrated_providers).

    Each entry is annotated with the provider route its cost-map tag resolves to (``family``), the
    same derivation registration applies — so the picker can show and reason about the route that
    will actually bill without re-implementing the tag→family normalization client-side.
    """
    try:
        catalog = await get_model_gateway_service(request).get_catalog()
    except ModelGatewayError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    return [{**entry, "family": route_family(entry.get("provider"))} for entry in catalog]


@router.get("/bedrock-regions", response_model=BedrockModelRegions)
async def bedrock_model_regions(
    model_id: str = Query(..., description="Bedrock model id or inference-profile id, as typed in the picker"),
    _: User = Depends(require_admin),
):
    """Which regions offer this Bedrock model — the question AWS's error refuses to answer.

    Advisory only: never blocks registration, and answers ``regions: null`` when the probe can't run
    (no ``bedrock:ListFoundationModels``, no credentials), in which case the UI shows nothing rather
    than claiming the model is unavailable. Long-cached server-side.
    """
    return BedrockModelRegions(
        model_id=model_id,
        regions=await model_regions(model_id),
        probed_regions=probed_regions(),
        gateway_region=config.model_gateway.default_bedrock_region,
    )


@router.get("/models/{model_name}/cost-prefill", response_model=CostPrefill)
async def cost_prefill(model_name: str, request: Request, db: DbSession, user: User = Depends(require_admin)):
    """Seed the rate-card form (best-effort).

    Prefers the model's stored rate card so EDITING a model starts from its real, previously-saved
    rates (they live in the rate card, not the gateway's model_info). Falls back to the gateway's
    known cost for models we don't bill yet (fresh registration). Empty when neither knows the
    model — the admin then enters rates manually.
    """
    try:
        model = await get_model_gateway_service(request).get_model(model_name)
    except ModelGatewayError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    info = (model or {}).get("model_info") or {}
    params = (model or {}).get("litellm_params") or {}

    # 1. Stored rate card — authoritative for already-billed models (the edit path). Keyed on the
    #    same provider family billing uses (see AGENTS.md provider-keying note): the runtime
    #    provider derived from the routing params. The gateway's litellm_provider (a cost-map
    #    implementation tag like `bedrock_converse`) is tried second, only so cards mis-keyed
    #    before this derivation existed still prefill their stored rates when edited.
    candidates = [_billing_provider(params), info.get("litellm_provider")]
    for provider in dict.fromkeys(p for p in candidates if p):
        rates = await request.app.state.rate_card_service.repository.get_all_active_rates(
            db=db, provider=provider, model_name=model_name
        )
        rate_pricing = {
            unit: RateCardPricingEntry(price_per_million=price, flow_direction=_UNIT_TO_FLOW.get(unit, "input"))
            for unit, price in rates.items()
            if price and price > 0
        }
        if rate_pricing:
            return CostPrefill(pricing=rate_pricing, source="rate_card")

    # 2. Fallback: the gateway's known cost (fresh registration, model not yet billed).
    pricing: dict[str, RateCardPricingEntry] = {}
    for cost_field, (unit, flow) in _COST_FIELD_TO_UNIT.items():
        val = info.get(cost_field)
        # ``is not None`` (not truthiness): a genuine 0.0 cost (free tier / 0.0 cache-read
        # rate) is a meaningful explicit-zero rate, not a "missing" value to drop.
        if val is not None:
            pricing[unit] = RateCardPricingEntry(
                price_per_million=Decimal(str(val)) * Decimal(1_000_000),
                flow_direction=flow,
            )

    # Web search is a per-query fee, not per-token: LiteLLM exposes it as
    # search_context_cost_per_query keyed by context size. We always call with
    # search_context_size="medium" (services.llm_gateway.gateway_web_search), so seed the
    # `web_search` unit from that tier (×1e6 for the per-1M rate card), falling back to low/high
    # only when medium is absent. This makes the search fee billable on registration like the
    # token costs — matching the `web_search` unit the proxy emits (custom_logger) — instead of
    # silently $0 until hand-entered. The rate card requires a positive price, so a 0.0 (free)
    # tier is left unpriced. NOTE: keep the tier in sync if gateway_web_search's size changes.
    search_costs = info.get("search_context_cost_per_query")
    if isinstance(search_costs, dict):
        per_query = search_costs.get("search_context_size_medium")
        if per_query is None:
            per_query = search_costs.get("search_context_size_low")
        if per_query is None:
            per_query = search_costs.get("search_context_size_high")
        if per_query and per_query > 0:
            pricing["web_search"] = RateCardPricingEntry(
                price_per_million=Decimal(str(per_query)) * Decimal(1_000_000),
                flow_direction="output",
            )

    return CostPrefill(pricing=pricing)


@router.post("/models", response_model=ModelRegistrationResponse, status_code=status.HTTP_201_CREATED)
async def register_model(
    request: Request,
    body: ModelRegistrationRequest,
    db: DbSession,
    user: User = Depends(require_admin),
):
    """Register a model: Rate Card first, then gateway routing/capability."""
    svc = get_model_gateway_service(request)

    # 0. One alias = one deployment. LiteLLM will happily hold several deployments under the same
    #    model_name and load-balance across them, but nothing in this console can express that: the
    #    rate card, the provider check and the role defaults are all keyed on the alias, edit/delete
    #    address a single gateway id, and edit_model already reports a leftover second deployment as
    #    a fault ("gateway will serve both until it is removed"). Registering a duplicate alias
    #    therefore silently doubles routing for a model the admin can only manage half of — refuse it
    #    BEFORE the rate-card write, so a rejected registration leaves nothing behind. Fails open when
    #    the gateway can't be listed; step 2 surfaces that as a 502 anyway.
    try:
        registered_aliases = {m.get("model_name") for m in await svc.list_models()}
    except ModelGatewayError:
        registered_aliases = set()
    if body.model_name in registered_aliases:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{body.model_name}' is already registered on the gateway. Aliases must be unique: "
                "edit the existing model to change its routing or pricing, remove it first, or pick a "
                "different alias."
            ),
        )

    # 1. Rate Card first — a model must be billable before it is usable.
    entry_ids, provider, litellm_params, model_info = await _write_rate_card_and_routing(request, body, db, user)

    # 2. Register on the gateway. If this fails, the Rate Card is a harmless orphan
    #    (the model is not usable because it isn't on the proxy) — surface the error.
    try:
        result = await svc.register_model(body.model_name, litellm_params, model_info)
    except ModelGatewayError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Rate card created but gateway registration failed ({e}); retry or clean up the rate card.",
        )

    gateway_model_id = _gateway_model_id(result)
    logger.info("Registered model %s (gateway id=%s) by %s", body.model_name, gateway_model_id, user.id)
    return ModelRegistrationResponse(
        model_name=body.model_name,
        rate_card_entry_ids=entry_ids,
        gateway_model_id=gateway_model_id,
        provider=provider,
    )


@router.put("/models/{model_id}", response_model=ModelRegistrationResponse)
async def edit_model(
    model_id: str,
    request: Request,
    body: ModelRegistrationRequest,
    db: DbSession,
    user: User = Depends(require_admin),
):
    """Edit a registered model's routing/capabilities/cost (db-backed models only).

    Mirrors registration: a new Rate Card version is written (pricing is kept
    time-versioned), then the gateway deployment is updated. LiteLLM rejects updates to
    config-defined models, so this only works for runtime-registered ones.
    """
    svc = get_model_gateway_service(request)

    # Same runtime-provider keying (and auto-prefixing) as register_model — an edit must
    # not re-key the rate card to a catalog tag the cost logger never emits.
    entry_ids, provider, litellm_params, model_info = await _write_rate_card_and_routing(request, body, db, user)

    try:
        result = await svc.update_model(model_id, body.model_name, litellm_params, model_info)
    except ModelGatewayError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Rate card updated but gateway update failed ({e}); retry.",
        )
    # update_model re-creates the deployment, so the gateway id changes.
    new_model_id = _gateway_model_id(result)
    # If the old deployment couldn't be deleted it lingers under the same model_name and the
    # gateway load-balances across both — the edit is only partially applied. Surface that as a
    # distinct status (instead of a clean "updated") so an admin knows to remove the stale one.
    stale_duplicate_id = result.get("_stale_duplicate_deployment_id") if isinstance(result, dict) else None
    if stale_duplicate_id:
        logger.warning(
            "Updated model %s (new gateway id=%s) but old deployment id=%s remains live "
            "(delete failed); gateway will serve both until it is removed.",
            body.model_name,
            new_model_id,
            stale_duplicate_id,
        )
    else:
        logger.info(
            "Updated model %s (old gateway id=%s, new gateway id=%s) by %s",
            body.model_name,
            model_id,
            new_model_id,
            user.id,
        )
    return ModelRegistrationResponse(
        model_name=body.model_name,
        rate_card_entry_ids=entry_ids,
        gateway_model_id=new_model_id,
        status="updated_with_stale_duplicate" if stale_duplicate_id else "updated",
        provider=provider,
    )


@router.post("/models/{model_name}/test")
async def test_model(model_name: str, request: Request, user: User = Depends(require_admin)):
    """Run a cheap call (chat or embedding, per the model's mode) to validate it end to end."""
    try:
        await get_model_gateway_service(request).test_model(model_name)
    except ModelGatewayError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Test call failed: {e}")
    return {"status": "ok", "model_name": model_name}


@router.post("/models/{model_id}/default")
async def set_default(
    model_id: str,
    body: SetDefaultRequest,
    request: Request,
    db: DbSession,
    user: User = Depends(require_admin),
):
    """Set a model as the fleet default for its role (graceful degradation).

    The default (role → alias) is stored in our DB — not the gateway — because LiteLLM's
    /model/update can't persist a custom flag. The apps read it from /api/v1/models/defaults
    and fall back to it when a referenced alias has been retired.
    """
    model = await get_model_gateway_service(request).get_model_by_id(model_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Model id {model_id} not registered")
    alias = model.get("model_name") or ""
    defaults_service = get_model_defaults_service(request)
    # The audited repository records this fleet-wide config change and commits
    # (AGENTS.md: admin writes go through the repository pattern → automatic audit).
    try:
        await defaults_service.set_default(db, actor=user, role=body.role, model_alias=alias)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info("Set '%s' (id=%s) as default for role=%s by %s", alias, model_id, body.role, user.id)
    return {"status": "ok", "model_name": alias, "default_for": body.role}


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(model_id: str, request: Request, user: User = Depends(require_admin)):
    """Remove a model from the gateway. The Rate Card is left for historical billing."""
    try:
        await get_model_gateway_service(request).delete_model(model_id)
    except ModelGatewayError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    logger.info("Deleted gateway model id=%s by %s", model_id, user.id)
