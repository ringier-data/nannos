"""Models router — the model picker, served live from the Model Gateway.

With runtime registration the set of models is whatever is registered on the
proxy (DB-backed), so we read it live from `/model/info` rather than a static list.
A short in-process TTL cache keeps the per-request cost off the proxy.
"""

import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..db.session import DbSession
from ..dependencies import require_auth_or_bearer_token
from ..models.user import User
from ..services.model_gateway_service import ModelGatewayError, thinking_levels_for

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/api/v1", tags=["models"])

_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, tuple[float, list]] = {}


def _price_per_million(cost_per_token: object) -> float | None:
    """Gateway cost-per-token → list price per 1M tokens (USD), or None if unpriced."""
    if cost_per_token is None:
        return None
    try:
        return round(float(cost_per_token) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


class AvailableModel(BaseModel):
    value: str
    label: str
    provider: str
    supports_thinking: bool = False
    thinking_levels: list[str] | None = None
    is_default: bool = False
    # Gateway list price per 1M tokens (USD), or None when the gateway has no price for
    # the model. Informational — for model-selection guidance, not authoritative billing
    # (rate cards remain the billing source of truth).
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None


def _to_available(model: dict, default_model: str) -> AvailableModel | None:
    """Map a gateway /model/info entry → picker model. Skips non-chat (e.g. embeddings)."""
    info = model.get("model_info") or {}
    if info.get("mode") and info.get("mode") != "chat":
        return None
    name = model.get("model_name", "")
    levels = thinking_levels_for(info)
    return AvailableModel(
        value=name,
        label=info.get("label") or name,
        provider=info.get("provider") or info.get("litellm_provider") or "Model Gateway",
        supports_thinking=bool(levels),
        thinking_levels=levels or None,
        is_default=(name == default_model),
        input_price_per_million=_price_per_million(info.get("input_cost_per_token")),
        output_price_per_million=_price_per_million(info.get("output_cost_per_token")),
    )


@router.get("/models", response_model=list[AvailableModel], tags=["MCP"], operation_id="console_list_models")
async def list_available_models(request: Request, db: DbSession, _user: User = Depends(require_auth_or_bearer_token)):
    """List the LLM models currently registered on the Model Gateway, with capabilities.

    Returns, per model: ``value`` (the model alias to use), ``label``, ``provider``,
    ``supports_thinking`` / ``thinking_levels`` (extended-thinking support),
    ``is_default`` (the platform default chat model), and ``input_price_per_million`` /
    ``output_price_per_million`` (the gateway list price in USD per 1M tokens, or null when
    unpriced — informational, for model selection, not authoritative billing). Read live
    from the gateway (cached ~30s), so it always reflects the models actually available —
    use it to pick an appropriate model when creating or updating a sub-agent.

    Also exposed as the ``console_list_models`` MCP tool. Any authenticated user may read it
    (model selection isn't admin-only — regular users pick models when creating sub-agents);
    management (register/edit/delete) stays admin-only."""
    now = time.monotonic()
    cached = _cache.get("models")
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    service = request.app.state.model_gateway_service
    try:
        raw = await service.list_models()
    except ModelGatewayError as e:
        if cached:  # serve stale on transient gateway errors
            logger.warning("Gateway list failed (%s); serving stale model list", e)
            return cached[1]
        raise HTTPException(status_code=503, detail="Model Gateway unavailable") from e

    # The authoritative chat default is the admin-editable model_defaults store (what the
    # apps actually resolve via agent-common); env is only the fallback. Reading env here
    # would badge a different model than apps use whenever an admin sets the default.
    defaults = await request.app.state.model_defaults_service.get_all(db)
    default_model = defaults.get("chat") or os.getenv("DEFAULT_MODEL", "claude-sonnet-4.5")
    models = [m for m in (_to_available(d, default_model) for d in raw) if m is not None]
    _cache["models"] = (now, models)
    return models


@router.get("/models/defaults")
async def model_defaults(request: Request, db: DbSession) -> dict[str, str]:
    """Fleet default model alias per role (chat / embedding / multimodal_embedding).

    Read by the apps (agent-common) for graceful degradation when a referenced alias has
    been retired, and by the console to badge the default. Unauthenticated like /models —
    the data is non-sensitive (alias names) and the route is in-cluster only.
    """
    return await request.app.state.model_defaults_service.get_all(db)


@router.get("/models/embeddings/status")
async def embedding_status(request: Request, db: DbSession) -> dict:
    """Whether catalog embedding is actually usable (default set AND registered on the gateway).

    The catalog UI gates on this rather than on "a default row exists" — a default pointing at
    a retired/unregistered model is the silent-misconfiguration case (a stale catalog looks
    healthy while indexing/search can't run). See feature_status.get_embedding_readiness.
    """
    from ..services.feature_status import get_embedding_readiness

    status, alias, reason = await get_embedding_readiness(request, db)
    return {"ready": status == "ready", "status": status, "model": alias, "reason": reason}
