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


def _provider_error_detail(resp: httpx.Response) -> str:
    """Pull a concise, human-readable reason out of a LiteLLM/provider error response.

    LiteLLM wraps provider errors as ``{"error": {"message": ...}}``; the message often quotes the
    provider verbatim (e.g. a Vertex 404 naming the model + location). Returns a trimmed message,
    or '' when none is parseable. Safe to surface to admins ONLY for calls that carry no credentials
    (test/inference) — never for register/update, whose errors can echo the submitted secrets.
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:300]
    err = body.get("error", body) if isinstance(body, dict) else body
    msg = err.get("message") if isinstance(err, dict) else None
    return (msg or "").strip()[:300]


# LiteLLM's bundled model catalog (cost + capabilities for 100+ models). Pin the ref
# to the deployed proxy version for accuracy; overridable via env.
_COSTMAP_REF = os.getenv("LITELLM_COSTMAP_REF", "main")
_COSTMAP_URL = f"https://raw.githubusercontent.com/BerriAI/litellm/{_COSTMAP_REF}/model_prices_and_context_window.json"
_CATALOG_TTL = 6 * 3600.0
# A real cost map has thousands of provider-tagged entries. Requiring a floor of recognizable ones is
# how we notice that the payload's SHAPE changed (a wrapper object, a renamed key, an error page that
# happens to be valid JSON) rather than parsing it into a near-empty catalog and reporting that as
# fact — an empty catalog reads as "unreadable" downstream and blocks registrations. Counted on the
# RAW map, before our integrated-provider filter, so a deployment that integrates one provider isn't
# mistaken for a broken payload.
_MIN_COST_MAP_ENTRIES = 50


def _looks_like_cost_map(raw: object) -> bool:
    """Is this payload a LiteLLM cost map we can parse (id → {litellm_provider, …})?

    Reads every entry defensively: this runs on data fetched from upstream, so a single hostile or
    surprising value must not turn the shape CHECK into the failure it exists to detect.
    """
    if not isinstance(raw, dict) or len(raw) < _MIN_COST_MAP_ENTRIES:
        return False
    tagged = 0
    for info in raw.values():
        try:
            if isinstance(info, dict) and info.get("litellm_provider"):
                tagged += 1
        except Exception:  # noqa: S112 - an unreadable entry simply doesn't count as evidence
            continue
    return tagged >= _MIN_COST_MAP_ENTRIES
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

    An explicitly-stored ``supports_reasoning: False`` is an admin override: the console writes
    the capability booleans into the deployment's model_info, which shadows the cost map (the
    proxy's /model/info merge only fills keys the deployment doesn't set). It turns thinking
    off outright — even if the cost map enumerates per-effort flags for the underlying model.
    """
    if info.get("supports_reasoning") is False:
        return []
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
        expose_error: bool = False,
    ) -> dict:
        """Call the gateway management API. ``optional=True`` marks an endpoint that may not
        exist on every proxy version (the caller has a fallback): its failures are logged at
        debug, not error, so an expected 404 isn't surfaced as noise.

        Provider credentials only ever travel inside ``litellm_params`` (api_key,
        aws_secret_access_key, vertex_credentials, …). LiteLLM validation errors can reflect the
        submitted payload, so whenever the request body carries ``litellm_params`` the response
        body is suppressed from logs — derived from the payload, not a per-call flag, so a future
        credential-bearing endpoint is covered automatically and can't forget to opt in.

        ``expose_error=True`` additionally returns the provider's error *message* in the raised
        exception (for the admin UI). Only safe for calls whose request carries no credentials
        AND whose error bodies are plain provider/inference errors (the model-test path) — it is
        ignored for credential-bearing requests, which always stay opaque."""
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
            status = e.response.status_code
            if carries_credentials:
                # Register/update echo the submitted litellm_params (incl. secrets) on validation
                # errors, so the body never reaches logs or the admin. Opaque, expose_error ignored.
                log("Gateway %s %s → %s (body suppressed: may echo credentials)", method, path, status)
                raise ModelGatewayError(f"Gateway returned {status}") from e
            # No credentials in this request — log the truncated body for diagnosis (unchanged).
            log("Gateway %s %s → %s: %s", method, path, status, e.response.text[:300])
            # Surface the provider's reason to the admin only when the caller opted in (model test):
            # those errors are plain inference failures (e.g. wrong vertex_location → 404), no secrets.
            detail = _provider_error_detail(e.response) if expose_error else ""
            raise ModelGatewayError(f"Gateway returned {status}" + (f": {detail}" if detail else "")) from e
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

    async def update_model(
        self, model_id: str, model_name: str, litellm_params: dict, model_info: dict | None = None
    ) -> dict:
        """Edit a registered deployment by re-creating it (register new, then delete old).

        LiteLLM's /model/update does NOT persist custom model_info keys (input_modes, mode,
        the default flag, …) — only /model/new does (see model_defaults_service). So a plain
        /model/update silently drops our capability metadata, leaving edits (e.g. adding the
        'file' input mode) with no runtime effect. Re-registering forces model_info to stick.

        Register-before-delete avoids a window where the alias has no live deployment; LiteLLM
        allows multiple deployments per public model_name, so the brief overlap is safe. Returns
        the newly registered deployment (carrying the NEW gateway model id).

        If deleting the old deployment fails, the re-registration still stands but a stale
        duplicate remains live under the same public model_name — the gateway will load-balance
        across both, so the edit is only partially applied until the old one is removed. That is
        signalled to the caller via ``_stale_duplicate_deployment_id`` on the returned dict (a
        private key, never serialized to the API client) so the endpoint can surface it rather
        than reporting a clean success.
        """
        result = await self.register_model(model_name, litellm_params, model_info)
        try:
            await self.delete_model(model_id)
        except ModelGatewayError:
            logger.warning(
                "update_model: re-registered '%s' but failed to delete old deployment id %s; "
                "a duplicate deployment may remain — delete it manually.",
                model_name,
                model_id,
            )
            if isinstance(result, dict):
                result["_stale_duplicate_deployment_id"] = model_id
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

        Source: the public cost map at LITELLM_COSTMAP_REF (freshest — new models land there before
        the proxy image is upgraded), falling back to the proxy's own bundled map when egress is
        unavailable. Cached; returns [] only when neither answers — and "[]" is load-bearing
        elsewhere: registration reads it to tell "unknown model id" (422) from "catalog unreadable"
        (502), and the provider config check suppresses its unresolved-route findings when it is empty.
        """
        now = time.monotonic()
        if self._catalog_cache and now - self._catalog_cache[0] < _CATALOG_TTL:
            return self._catalog_cache[1]

        # Two sources, and the order is a deliberate trade, not a preference:
        #  1. the public JSON (LITELLM_COSTMAP_REF, default `main`) — the FRESHEST view of what
        #     providers offer. This is what the picker is for: a model released today appears here
        #     while the proxy image is still on an older litellm. Registering it is safe even then,
        #     because an id we prefix with its route (`bedrock/…`) is routed generically — the proxy
        #     doesn't need the entry to serve the call. Unknown TAGS can't leak into billing either:
        #     `route_family` maps anything it doesn't recognize to None → registration 422s.
        #  2. the proxy's OWN bundled map — same data as of its image version, no egress needed.
        # Each source is tried END TO END (fetch + shape check + normalize) and any failure moves on
        # to the next: `main` is an upstream file nobody here controls, so it can also change shape
        # under us, and a parse error must not be a worse outcome than being offline. The fallback
        # matters more than it looks: `[]` is load-bearing (registration's 422-vs-502 split, the
        # provider check's unresolved-route suppression, unprefixed-id resolution), so a bad payload
        # or lost egress must degrade to the proxy's slightly older catalog, never to "no catalog".
        for source, load in (
            ("public cost map", self._fetch_public_cost_map),
            ("gateway cost map", self._fetch_proxy_cost_map),
        ):
            try:
                raw = await load()
                if not _looks_like_cost_map(raw):
                    logger.warning("%s: not a usable cost map (unexpected shape) — trying the next source", source)
                    continue
                catalog = self._normalize_catalog(raw)
            except Exception as e:  # unreachable, undecodable, or a shape our parser can't handle
                logger.warning("%s unusable (%s: %s) — trying the next source", source, type(e).__name__, e)
                continue
            self._catalog_cache = (now, catalog)
            return catalog

        logger.warning("Could not load a model catalog from any source")
        return self._catalog_cache[1] if self._catalog_cache else []

    async def _fetch_public_cost_map(self) -> object:
        """The upstream cost map JSON at LITELLM_COSTMAP_REF (raises on HTTP or decode failure)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_COSTMAP_URL)
            resp.raise_for_status()
            return resp.json()

    async def _fetch_proxy_cost_map(self) -> object:
        """The proxy's own bundled cost map, from whichever route this LiteLLM version exposes.

        Renamed upstream (1.90.0 serves /public/…, older builds /get/…), so try both; a 404 on either
        is expected and ``optional=True`` keeps it out of the error log.
        """
        for route in ("/public/litellm_model_cost_map", "/get/litellm_model_cost_map"):
            try:
                raw = await self._request("GET", route, optional=True)
            except ModelGatewayError:
                continue
            if isinstance(raw, dict) and raw:
                return raw
        return None

    def _normalize_catalog(self, raw: dict) -> list[dict]:
        """Cost-map entries → picker entries, pre-filtered to the providers this deployment integrated.

        Per-entry failures are skipped rather than fatal: upstream adds fields and occasionally changes
        a type, and one odd entry must not cost us the other three thousand.
        """
        allowed = set(config.model_gateway.integrated_providers)
        catalog: list[dict] = []
        skipped = 0
        for key, info in raw.items():
            if key == "sample_spec" or not isinstance(info, dict):
                continue
            try:
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
                        "input_cost_per_image": info.get("input_cost_per_image"),
                        "output_cost_per_token": info.get("output_cost_per_token"),
                        "cache_read_input_token_cost": info.get("cache_read_input_token_cost"),
                        "cache_creation_input_token_cost": info.get("cache_creation_input_token_cost"),
                        # Per-query web-search (grounding) fee, keyed by context size — lets the
                        # registration picker pre-fill the `web_search` rate-card unit on selection.
                        "search_context_cost_per_query": info.get("search_context_cost_per_query"),
                        "max_input_tokens": info.get("max_input_tokens"),
                        "supports_vision": info.get("supports_vision", False),
                        "supports_reasoning": info.get("supports_reasoning", False),
                        "supports_web_search": info.get("supports_web_search", False),
                        "supports_audio_input": info.get("supports_audio_input", False),
                        "supports_pdf_input": info.get("supports_pdf_input", False),
                    }
                )
            except Exception as e:
                skipped += 1
                logger.debug("Skipping catalog entry %r: %s: %s", key, type(e).__name__, e)
        if skipped:
            logger.warning("Skipped %d unparseable catalog entr%s", skipped, "y" if skipped == 1 else "ies")
        return catalog

    async def catalog_model(self, model_id: str) -> dict | None:
        """The catalog entry for this exact cost-map id, or None (unknown / filtered / unreadable).

        Registration uses it to resolve the provider family of an *unprefixed* catalog id — the norm
        for Bedrock, whose cost-map keys are bare (`eu.amazon.nova-2-lite-v1:0`) — so the client
        never has to send a provider value at all. ``get_catalog`` keys on the cost-map key, so this
        is an exact match, and its cache is normally already warm from the picker's own fetch.
        """
        return next((c for c in await self.get_catalog() if c.get("model_id") == model_id), None)

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
            return await self._request("POST", "/v1/embeddings", json=body, timeout=30.0, expose_error=True)
        return await self._request(
            "POST",
            "/v1/chat/completions",
            json={"model": model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
            timeout=30.0,
            expose_error=True,
        )
