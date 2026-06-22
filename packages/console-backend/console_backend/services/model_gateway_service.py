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
# Short TTL for the /model/info deployment list. Long enough to collapse the 2-3 repeated
# fetches a single request fans out (System Status page; get_model/get_model_by_id lookups),
# short enough that a write by another replica self-heals quickly. Our own writes invalidate
# it synchronously (see register/update/delete_model).
_LIST_TTL = 10.0


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
        self._list_cache: tuple[float, list[dict]] | None = None
        # One pooled client reused across every management call (the service is a process-wide
        # singleton). Created lazily on first use so it binds to the running event loop; opening
        # a fresh AsyncClient per call meant a new TCP+TLS handshake every time.
        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._master_key}", "Content-Type": "application/json"}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the pooled client on app shutdown."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        timeout: float | None = None,
        optional: bool = False,
    ) -> dict:
        """Call the gateway management API. ``optional=True`` marks an endpoint that may not
        exist on every proxy version (the caller has a fallback): its failures are logged at
        debug, not error, so an expected 404 isn't surfaced as noise.

        Provider credentials only ever travel inside ``litellm_params`` (api_key,
        aws_secret_access_key, vertex_credentials, …). LiteLLM validation errors can reflect the
        submitted payload, so whenever the request body carries ``litellm_params`` the response
        body is suppressed from logs — derived from the payload, not a per-call flag, so a future
        credential-bearing endpoint is covered automatically and can't forget to opt in."""
        carries_credentials = isinstance(json, dict) and "litellm_params" in json
        try:
            client = self._get_client()
            resp = await client.request(
                method, f"{self._base_url}{path}", headers=self._headers(), json=json, timeout=timeout or self._timeout
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as e:
            log = logger.debug if optional else logger.error
            if carries_credentials:
                log("Gateway %s %s → %s (body suppressed: may echo credentials)", method, path, e.response.status_code)
            else:
                log("Gateway %s %s → %s: %s", method, path, e.response.status_code, e.response.text[:300])
            raise ModelGatewayError(f"Gateway returned {e.response.status_code}") from e
        except httpx.HTTPError as e:
            log = logger.debug if optional else logger.error
            log("Gateway %s %s unreachable: %s", method, path, e)
            raise ModelGatewayError("Gateway unreachable") from e

    async def list_models(self) -> list[dict]:
        """All registered deployments with their litellm_params + model_info.

        Cached for _LIST_TTL so the several lookups a single request fans out (System Status,
        get_model/get_model_by_id/thinking_capable_aliases) share one /model/info fetch instead
        of each re-listing. Our own writes invalidate the cache synchronously."""
        now = time.monotonic()
        if self._list_cache and now - self._list_cache[0] < _LIST_TTL:
            return self._list_cache[1]
        data = await self._request("GET", "/model/info")
        models = data.get("data", data if isinstance(data, list) else [])
        self._list_cache = (now, models)
        return models

    def _invalidate_list_cache(self) -> None:
        """Drop the cached deployment list after a write so the next read reflects it."""
        self._list_cache = None

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
        result = await self._request(
            "POST",
            "/model/new",
            json={"model_name": model_name, "litellm_params": litellm_params, "model_info": model_info or {}},
        )
        self._invalidate_list_cache()
        return result

    async def update_model(self, model_id: str, litellm_params: dict, model_info: dict | None = None) -> dict:
        payload = {"litellm_params": litellm_params, "model_info": {**(model_info or {}), "id": model_id}}
        result = await self._request("POST", "/model/update", json=payload)
        self._invalidate_list_cache()
        return result

    async def delete_model(self, model_id: str) -> None:
        await self._request("POST", "/model/delete", json={"id": model_id})
        self._invalidate_list_cache()

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

        Shape-aware for embeddings: the ping carries the same ``dimensions`` param the runtime
        adapter would send for this model's profile, so a model that rejects the Matryoshka
        param fails *registration* instead of passing here and crashing mid-sync (the runtime
        always requested ``dimensions`` regardless of provider — the gap this closes).
        """
        from ringier_a2a_sdk.embeddings import _DEFAULT_DIMENSION, profile_for

        model = await self.get_model(model_name)
        mode = ((model or {}).get("model_info") or {}).get("mode", "chat")
        if mode == "embedding":
            litellm_model = ((model or {}).get("litellm_params") or {}).get("model")
            provider = ((model or {}).get("model_info") or {}).get("litellm_provider")
            body: dict = {"model": model_name, "input": ["ping"]}
            if profile_for(litellm_model, provider).send_dimensions:
                body["dimensions"] = _DEFAULT_DIMENSION
            return await self._request("POST", "/v1/embeddings", json=body, timeout=30.0)
        return await self._request(
            "POST",
            "/v1/chat/completions",
            json={"model": model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
            timeout=30.0,
        )
