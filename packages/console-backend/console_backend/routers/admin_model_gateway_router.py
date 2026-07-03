"""Admin router for runtime model registration via the Model Gateway.

console-backend is the single front door for adding/editing models: it writes the
billing Rate Card (authoritative billed rate) and registers routing+capability on
the LiteLLM proxy. The Rate Card is written FIRST so a model is
never usable before it is billable. Master-key access stays server-side.
"""

import logging
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..config import config
from ..db.session import DbSession
from ..dependencies import require_admin
from ..models.model_gateway import (
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
from ..services.model_defaults_service import ModelDefaultsService
from ..services.model_gateway_service import ModelGatewayError, ModelGatewayService

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
                provider=info.get("litellm_provider") or params.get("custom_llm_provider"),
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
    )


@router.get("/catalog", response_model=list[CatalogModel])
async def model_catalog(request: Request, user: User = Depends(require_admin)):
    """LiteLLM's known-model catalog for the registration picker, pre-filtered to the
    providers this deployment has integrated (config.model_gateway.integrated_providers)."""
    try:
        catalog = await get_model_gateway_service(request).get_catalog()
    except ModelGatewayError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    return catalog


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
    #    same provider family billing uses (see AGENTS.md provider-keying note).
    provider = info.get("litellm_provider") or params.get("custom_llm_provider")
    if provider:
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
    rate_card_service = request.app.state.rate_card_service

    # 1. Rate Card first — a model must be billable before it is usable.
    entry_ids = await rate_card_service.create_model_rate_card(
        db=db,
        actor=user,
        provider=body.provider,
        model_name=body.model_name,
        pricing=body.pricing,
        model_name_pattern=body.model_name_pattern,
    )
    await db.commit()

    # 2. Register on the gateway. If this fails, the Rate Card is a harmless orphan
    #    (the model is not usable because it isn't on the proxy) — surface the error.
    # input_modes + mode are always written into model_info: input_modes so every
    # model declares its accepted payloads (orchestrator/sub-agents depend on it),
    # mode so chat vs embedding is explicit (the chat picker filters on mode=chat).
    model_info = {**body.model_info, "input_modes": body.input_modes, "mode": body.mode}
    litellm_params = _with_default_vertex_location(body.litellm_params, body.provider)
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
    rate_card_service = request.app.state.rate_card_service

    entry_ids = await rate_card_service.create_model_rate_card(
        db=db,
        actor=user,
        provider=body.provider,
        model_name=body.model_name,
        pricing=body.pricing,
        model_name_pattern=body.model_name_pattern,
    )
    await db.commit()

    model_info = {**body.model_info, "input_modes": body.input_modes, "mode": body.mode}
    litellm_params = _with_default_vertex_location(body.litellm_params, body.provider)
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
