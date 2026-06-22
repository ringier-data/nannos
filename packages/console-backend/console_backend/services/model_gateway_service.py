"""Client for the LiteLLM Model Gateway management API.

console-backend is the sole writer of the proxy's /model/* routes and holds the
master key server-side. This wraps the handful of management calls we need:
list/register/update/delete models, read capability+cost for pre-fill, and a
cheap test completion for the validation step.
"""

import logging
import os
import time

import httpx

from ..config import config

logger = logging.getLogger(__name__)

# LiteLLM's bundled model catalog (cost + capabilities for 100+ models). Pin the ref
# to the deployed proxy version for accuracy; overridable via env.
_COSTMAP_REF = os.getenv("LITELLM_COSTMAP_REF", "main")
_COSTMAP_URL = f"https://raw.githubusercontent.com/BerriAI/litellm/{_COSTMAP_REF}/model_prices_and_context_window.json"
_CATALOG_TTL = 6 * 3600.0


# LiteLLM reasoning_effort vocabulary in display order ("none" = off, covered by the
# enable-thinking toggle, so excluded here).
_EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh"]
# Offered only when a model declares it reasons but enumerates no per-effort support — the
# standard tiers essentially every reasoning model accepts. We can't infer finer than this
# without capability data, so this is a floor, never an over-claim of minimal/xhigh.
_BASELINE_EFFORTS = ["low", "medium", "high"]


def thinking_levels_for(info: dict) -> list[str]:
    """Reasoning efforts a model accepts, grounded in the gateway's capability flags.

    Single source of truth for "does this model support extended thinking": a non-empty
    return means yes. Shared by the model picker (models_router) and the sub-agent write
    path (sub_agent_service) so the UI and the persistence guard never disagree.

    Grounding rule: prefer the model's explicitly-declared ``supports_<effort>_reasoning_effort``
    flags and return exactly those — so a model that only supports e.g. "low" is no longer
    offered medium/high. Only when the model signals it reasons (``supports_reasoning`` or a
    bare none/max flag) but enumerates no usable per-effort detail do we fall back to the
    baseline tiers.
    """
    declared = [e for e in _EFFORT_ORDER if info.get(f"supports_{e}_reasoning_effort")]
    if declared:
        return declared
    # No usable per-effort detail: offer the baseline only if the model still says it reasons.
    has_reasoning = bool(info.get("supports_reasoning")) or any(
        info.get(f"supports_{e}_reasoning_effort") for e in ("none", "max")
    )
    return list(_BASELINE_EFFORTS) if has_reasoning else []


class ModelGatewayError(Exception):
    """Raised when the gateway management API returns an error."""


class ModelGatewayService:
    def __init__(self, base_url: str | None = None, master_key: str | None = None, timeout: float = 10.0):
        self._base_url = (base_url or config.model_gateway.url).rstrip("/")
        self._master_key = master_key if master_key is not None else config.model_gateway.master_key.get_secret_value()
        self._timeout = timeout
        self._catalog_cache: tuple[float, list[dict]] | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._master_key}", "Content-Type": "application/json"}

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, timeout: float | None = None, optional: bool = False
    ) -> dict:
        """Call the gateway management API. ``optional=True`` marks an endpoint that may not
        exist on every proxy version (the caller has a fallback): its failures are logged at
        debug, not error, so an expected 404 isn't surfaced as noise."""
        try:
            async with httpx.AsyncClient(timeout=timeout or self._timeout) as client:
                resp = await client.request(method, f"{self._base_url}{path}", headers=self._headers(), json=json)
                resp.raise_for_status()
                return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as e:
            log = logger.debug if optional else logger.error
            log("Gateway %s %s → %s: %s", method, path, e.response.status_code, e.response.text[:300])
            raise ModelGatewayError(f"Gateway returned {e.response.status_code}") from e
        except httpx.HTTPError as e:
            log = logger.debug if optional else logger.error
            log("Gateway %s %s unreachable: %s", method, path, e)
            raise ModelGatewayError("Gateway unreachable") from e

    async def list_models(self) -> list[dict]:
        """All registered deployments with their litellm_params + model_info."""
        data = await self._request("GET", "/model/info")
        return data.get("data", data if isinstance(data, list) else [])

    async def get_model(self, model_name: str) -> dict | None:
        for m in await self.list_models():
            if m.get("model_name") == model_name:
                return m
        return None

    async def thinking_capable_aliases(self) -> set[str]:
        """Aliases of registered models that support extended thinking, live from the gateway.

        The authoritative answer to "which models support thinking" — same derivation the
        model picker uses (see thinking_levels_for). Used by the sub-agent write path so a
        thinking config is persisted iff the gateway actually reports the model supports it.
        """
        return {
            m["model_name"]
            for m in await self.list_models()
            if m.get("model_name") and thinking_levels_for(m.get("model_info") or {})
        }

    async def register_model(self, model_name: str, litellm_params: dict, model_info: dict | None = None) -> dict:
        return await self._request(
            "POST",
            "/model/new",
            json={"model_name": model_name, "litellm_params": litellm_params, "model_info": model_info or {}},
        )

    async def update_model(self, model_id: str, litellm_params: dict, model_info: dict | None = None) -> dict:
        payload = {"litellm_params": litellm_params, "model_info": {**(model_info or {}), "id": model_id}}
        return await self._request("POST", "/model/update", json=payload)

    async def delete_model(self, model_id: str) -> None:
        await self._request("POST", "/model/delete", json={"id": model_id})

    async def get_model_by_id(self, model_id: str) -> dict | None:
        """The registered deployment with this gateway id, or None."""
        for m in await self.list_models():
            if (m.get("model_info") or {}).get("id") == model_id:
                return m
        return None

    async def get_catalog(self) -> list[dict]:
        """LiteLLM's known-model catalog (cost + capabilities), normalized for the picker.

        Source: the proxy's bundled cost map if exposed, else the pinned public JSON.
        Cached; returns [] on failure (admin can still enter a model id manually).
        """
        now = time.monotonic()
        if self._catalog_cache and now - self._catalog_cache[0] < _CATALOG_TTL:
            return self._catalog_cache[1]

        raw: dict | None = None
        try:  # version-accurate + internal IF this proxy version exposes it (older route);
            # newer LiteLLM moved/removed it, so a 404 here is expected → fall back to the
            # pinned public cost map below. optional=True keeps that 404 out of the error log.
            raw = await self._request("GET", "/get/litellm_model_cost_map", optional=True)
        except ModelGatewayError:
            raw = None
        if not isinstance(raw, dict) or not raw:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(_COSTMAP_URL)
                    resp.raise_for_status()
                    raw = resp.json()
            except httpx.HTTPError as e:
                logger.warning("Could not load model catalog: %s", e)
                return self._catalog_cache[1] if self._catalog_cache else []

        # Pre-filter to the providers this deployment has integrated (creds on the proxy).
        allowed = set(config.model_gateway.integrated_providers)
        catalog: list[dict] = []
        for key, info in raw.items():
            if key == "sample_spec" or not isinstance(info, dict):
                continue
            mode = info.get("mode", "chat")
            if mode not in ("chat", "embedding"):
                continue  # focus on what we register (chat + embeddings)
            if allowed and info.get("litellm_provider") not in allowed:
                continue
            catalog.append(
                {
                    "model_id": key,
                    "provider": info.get("litellm_provider"),
                    "mode": mode,
                    "input_cost_per_token": info.get("input_cost_per_token"),
                    "output_cost_per_token": info.get("output_cost_per_token"),
                    "cache_read_input_token_cost": info.get("cache_read_input_token_cost"),
                    "cache_creation_input_token_cost": info.get("cache_creation_input_token_cost"),
                    "max_input_tokens": info.get("max_input_tokens"),
                    "supports_vision": info.get("supports_vision", False),
                    "supports_reasoning": info.get("supports_reasoning", False),
                    "supports_audio_input": info.get("supports_audio_input", False),
                    "supports_pdf_input": info.get("supports_pdf_input", False),
                }
            )
        self._catalog_cache = (now, catalog)
        return catalog

    async def test_model(self, model_name: str) -> dict:
        """Cheap call to validate a freshly-registered model end to end.

        Mode-aware: embedding models must be hit on /v1/embeddings — sending them a chat
        payload makes the provider reject the request (e.g. Bedrock Titan errors on the
        chat-only `textGenerationConfig` key), which would wrongly fail registration.
        """
        model = await self.get_model(model_name)
        mode = ((model or {}).get("model_info") or {}).get("mode", "chat")
        if mode == "embedding":
            return await self._request(
                "POST",
                "/v1/embeddings",
                json={"model": model_name, "input": ["ping"]},
                timeout=30.0,
            )
        return await self._request(
            "POST",
            "/v1/chat/completions",
            json={"model": model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
            timeout=30.0,
        )
