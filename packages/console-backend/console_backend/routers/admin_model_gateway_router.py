"""Admin router for runtime model registration via the Model Gateway.

console-backend is the single front door for adding/editing models: it writes the
billing Rate Card (authoritative billed rate) and registers routing+capability on
the LiteLLM proxy. The Rate Card is written FIRST so a model is
never usable before it is billable. Master-key access stays server-side.
"""

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..db.session import DbSession
from ..dependencies import require_admin
from ..models.model_gateway import (
    CatalogModel,
    CostPrefill,
    GatewayModel,
    ModelRegistrationRequest,
    ModelRegistrationResponse,
    SetDefaultRequest,
)
from ..models.usage import RateCardPricingEntry
from ..models.user import User
from ..services.model_defaults_service import ModelDefaultsService
from ..services.model_gateway_service import ModelGatewayError, ModelGatewayService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/model-gateway", tags=["admin-model-gateway"])

# cost-per-token (gateway) → price-per-million (rate card), with the billing-unit
# names the proxy CustomLogger emits.
_COST_FIELD_TO_UNIT = {
    "input_cost_per_token": ("base_input_tokens", "input"),
    "output_cost_per_token": ("base_output_tokens", "output"),
    "cache_read_input_token_cost": ("cache_read_input_tokens", "input"),
    "cache_creation_input_token_cost": ("cache_creation_input_tokens", "input"),
}


def _with_provider_creds(litellm_params: dict) -> dict:
    """Inject proxy-side credential refs the registration form doesn't supply.

    Runtime-registered Vertex models must carry ``vertex_credentials`` so the proxy resolves
    GCP creds from its ``GCP_KEY`` env — ADC is intentionally not wired (it hangs the proxy's
    startup health check). Config-defined Vertex models already set this; the registration form
    only sends vertex_location/vertex_project, so without this a runtime Vertex model falls back
    to ADC and its test ping fails with "default credentials were not found".
    """
    params = dict(litellm_params)
    if str(params.get("model", "")).startswith("vertex_ai/") and "vertex_credentials" not in params:
        params["vertex_credentials"] = "os.environ/GCP_KEY"
    return params


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
                input_cost_per_token=info.get("input_cost_per_token"),
                output_cost_per_token=info.get("output_cost_per_token"),
                supports_reasoning=info.get("supports_reasoning"),
                supports_vision=info.get("supports_vision"),
            )
        )
    return out


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
async def cost_prefill(model_name: str, request: Request, user: User = Depends(require_admin)):
    """Seed the rate-card form from the gateway's known cost (best-effort).

    Returns empty pricing when the gateway doesn't know the model (bleeding-edge) —
    the admin then enters rates manually.
    """
    try:
        model = await get_model_gateway_service(request).get_model(model_name)
    except ModelGatewayError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    info = (model or {}).get("model_info") or {}
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
    try:
        result = await svc.register_model(body.model_name, _with_provider_creds(body.litellm_params), model_info)
    except ModelGatewayError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Rate card created but gateway registration failed ({e}); retry or clean up the rate card.",
        )

    gateway_model_id = (result.get("model_info") or {}).get("id") if isinstance(result, dict) else None
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
    try:
        await svc.update_model(model_id, _with_provider_creds(body.litellm_params), model_info)
    except ModelGatewayError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Rate card updated but gateway update failed ({e}); retry.",
        )
    logger.info("Updated model %s (gateway id=%s) by %s", body.model_name, model_id, user.id)
    return ModelRegistrationResponse(
        model_name=body.model_name,
        rate_card_entry_ids=entry_ids,
        gateway_model_id=model_id,
        status="updated",
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
