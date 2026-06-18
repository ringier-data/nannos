"""Client for the LiteLLM Model Gateway management API (ADR-0001/0005).

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

    async def _request(self, method: str, path: str, *, json: dict | None = None, timeout: float | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout or self._timeout) as client:
                resp = await client.request(method, f"{self._base_url}{path}", headers=self._headers(), json=json)
                resp.raise_for_status()
                return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as e:
            logger.error("Gateway %s %s → %s: %s", method, path, e.response.status_code, e.response.text[:300])
            raise ModelGatewayError(f"Gateway returned {e.response.status_code}") from e
        except httpx.HTTPError as e:
            logger.error("Gateway %s %s unreachable: %s", method, path, e)
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
        try:  # version-accurate + internal if the proxy exposes it
            raw = await self._request("GET", "/get/litellm_model_cost_map")
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

    async def test_completion(self, model_name: str) -> dict:
        """Cheap completion to validate a freshly-registered model end to end."""
        return await self._request(
            "POST",
            "/v1/chat/completions",
            json={"model": model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
            timeout=30.0,
        )
