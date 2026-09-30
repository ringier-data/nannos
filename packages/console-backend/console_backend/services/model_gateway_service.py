"""Client for the LiteLLM Model Gateway management API.

console-backend is the sole writer of the proxy's /model/* routes and holds the
master key server-side. This wraps the handful of management calls we need:
list/register/update/delete models, read capability+cost for pre-fill, and a
cheap test completion for the validation step.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from ringier_a2a_sdk.model_capabilities import (
    CAPABILITIES_KEY,
    FORCED_TOOL_CHOICE,
    RESPONSE_FORMAT,
    THINKING_OFF,
    THINKING_REPLAY,
    capabilities_of,
)

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
# The portable tiers: every reasoning provider accepts them, and LiteLLM's model map never flags
# them True — it only ever marks one False to exclude it. So they are offered to every reasoning
# model unless explicitly excluded. The complement (minimal/xhigh) must stay the keys of
# agent-common's `_NON_PORTABLE_EFFORT` (model_factory.py), defined there from the other side;
# test_thinking_levels.py pins the split so a new tier can't drift silently.
_PORTABLE_EFFORTS = {"low", "medium", "high"}


def thinking_levels_for(info: dict) -> list[str]:
    """Reasoning efforts a model accepts, grounded in the gateway's capability flags.

    Single source of truth for "does this model support extended thinking": a non-empty
    return means yes. Shared by the model picker (models_router) and the sub-agent write
    path (sub_agent_service) so the UI and the persistence guard never disagree.

    Grounding rule, matching how LiteLLM's model map uses the ``supports_<effort>_reasoning_effort``
    flags (it only ever flags the extra tiers True): a reasoning model gets the portable tiers
    (low/medium/high) unless one is flagged ``False``, and the non-portable ones (minimal/xhigh)
    only when flagged ``True``. agent-common gates only minimal/xhigh at request time; a portable
    tier passes through there, and the proxy's ``drop_params`` drops one the map excludes. A model
    reasons when it says so (``supports_reasoning``) or flags any tier; one that reasons but has no
    tier left (every portable one excluded, no extra flagged) keeps the portable tiers rather than
    silently losing thinking.

    An explicitly-stored ``supports_reasoning: False`` is an admin override: the console writes
    the capability booleans into the deployment's model_info, which shadows the cost map (the
    proxy's /model/info merge only fills keys the deployment doesn't set). It turns thinking
    off outright — even if the cost map enumerates per-effort flags for the underlying model.
    """
    if info.get("supports_reasoning") is False:
        return []
    flags = {e: info.get(f"supports_{e}_reasoning_effort") for e in (*_EFFORT_ORDER, "none", "max")}
    if not (info.get("supports_reasoning") or any(v is True for v in flags.values())):
        return []
    levels = [e for e in _EFFORT_ORDER if (flags[e] is not False if e in _PORTABLE_EFFORTS else flags[e] is True)]
    # An empty list reads as "no thinking" to the sub-agent write guard, which would switch thinking
    # off on the next save; a model that says it reasons keeps the portable tiers instead.
    return levels or [e for e in _EFFORT_ORDER if e in _PORTABLE_EFFORTS]


# The litellm_params that decide which endpoint answers: a record measured on one of them says
# nothing about another (a model's capabilities can differ by region or project).
_ROUTE_PARAMS = ("model", "aws_region_name", "vertex_location", "vertex_project", "api_base")


def _same_route(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in _ROUTE_PARAMS)


# Probe shape → the record key it decides (ringier_a2a_sdk.model_capabilities).
_SHAPE_KEYS = {
    "forced_tool_choice": FORCED_TOOL_CHOICE,
    "named_tool_choice": FORCED_TOOL_CHOICE,
    "response_format": RESPONSE_FORMAT,
    "thinking_off": THINKING_OFF,
    "thinking_replay": THINKING_REPLAY,
}


def _keys_for_shapes(shapes) -> set[str]:
    return {_SHAPE_KEYS[s] for s in shapes if s in _SHAPE_KEYS}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ModelGatewayError(Exception):
    """Raised when the gateway management API returns an error.

    ``status_code`` carries the proxy's HTTP status when there was one (None for a transport
    failure), so a caller can tell "the proxy said no such thing" from "the proxy is down" —
    a distinction the fallback routes need, since absent is their desired end state.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


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
                raise ModelGatewayError(f"Gateway returned {status}", status_code=status) from e
            # No credentials in this request — log the truncated body for diagnosis (unchanged).
            log("Gateway %s %s → %s: %s", method, path, status, e.response.text[:300])
            # Surface the provider's reason to the admin only when the caller opted in (model test):
            # those errors are plain inference failures (e.g. wrong vertex_location → 404), no secrets.
            detail = _provider_error_detail(e.response) if expose_error else ""
            raise ModelGatewayError(
                f"Gateway returned {status}" + (f": {detail}" if detail else ""), status_code=status
            ) from e
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
        # The registration probe's record (nannos#318) is not part of the edit form, so a
        # rebuilt model_info would silently drop it — and every reader would then treat a model
        # known to reject `response_format` as unprobed. Carry it over while the deployment
        # still points at the same provider model; a re-routed edit is a different model, and
        # the edit flow re-tests anyway.
        model_info = dict(model_info or {})
        if CAPABILITIES_KEY not in model_info:
            previous = await self.get_model_by_id(model_id)
            if previous and _same_route((previous.get("litellm_params") or {}), litellm_params):
                inherited = capabilities_of(previous.get("model_info"))
                if inherited:
                    model_info[CAPABILITIES_KEY] = inherited
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

    # --- Failover chains (nannos#204, ADR-0014) ------------------------------------------
    # LiteLLM stores fallbacks in its own DB when store_model_in_db is on, alongside the
    # model registry, so a chain cannot drift against the aliases it names. That is why the
    # chain is projected here rather than written into the proxy's config.yaml, which in
    # Nannos deliberately carries no model knowledge at all.

    async def set_fallbacks(self, model_name: str, fallback_models: list[str]) -> None:
        """Declare ``model_name``'s failover chain on the proxy (replaces any existing one).

        An empty chain deletes the entry rather than writing a zero-length one: LiteLLM
        treats a declared-but-empty fallback list as a configured route, and a stale empty
        route is harder to notice than no route.
        """
        if not fallback_models:
            await self.delete_fallbacks(model_name)
            return
        await self._request(
            "POST",
            "/fallback",
            json={
                "model": model_name,
                "fallback_models": list(fallback_models),
                "fallback_type": "general",
            },
        )

    async def delete_fallbacks(self, model_name: str) -> None:
        """Remove ``model_name``'s failover chain; a proxy holding no such entry is success.

        The 404 must be swallowed here rather than by ``optional=True``, which only lowers the
        log level and still raises. LiteLLM 404s ``DELETE /fallback/{model}`` when no entry
        exists — the common case (a tier whose chain has always been empty), and letting that
        propagate would abort a reprojection before it re-declared the new head's chain.
        """
        try:
            await self._request("DELETE", f"/fallback/{model_name}", optional=True)
        except ModelGatewayError as e:
            if e.status_code != 404:
                raise

    async def get_fallbacks(self, model_name: str) -> list[str]:
        """The failover chain the proxy currently holds for ``model_name`` (live, uncached).

        Read back from the proxy rather than from our own table so drift between the two is
        observable instead of assumed away.
        """
        try:
            data = await self._request("GET", f"/fallback/{model_name}", optional=True)
        except ModelGatewayError as e:
            if e.status_code == 404:
                return []  # no entry declared — a real, readable answer, not a failure
            raise
        models = data.get("fallback_models") or data.get("fallbacks") or []
        return [m for m in models if isinstance(m, str)]

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

    async def test_model(self, model_name: str, model_id: str | None = None) -> dict:
        """Validate a freshly-registered model end to end, and record what it accepts.

        Mode-aware: embedding models must be hit on /v1/embeddings — sending them a chat
        payload makes the provider reject the request (e.g. Bedrock Titan errors on the
        chat-only `textGenerationConfig` key), which would wrongly fail registration.

        Shape-aware for embeddings: the ping carries the same ``dimensions`` param the runtime
        adapter would send for this model's profile, so a model that rejects the Matryoshka
        param fails *registration* instead of passing here and crashing mid-sync (the runtime
        always requested ``dimensions`` regardless of provider — the gap this closes).

        Shape-aware for chat (nannos#318): not a ping but the request shapes the harness
        actually sends (``ringier_a2a_sdk.model_capabilities``). A shape every agent turn
        needs failing raises — registration is refused with the provider's reason; one that
        could not be measured (a transient failure) raises too, as "inconclusive", so nothing
        is written and the admin re-tests. A shape the harness can route around failing is
        recorded on the deployment's ``model_info`` under ``nannos_capabilities`` for the
        gateway hook and the app to act on, and the report is returned for the admin.

        ``model_id`` pins the deployment the record is written to (the caller has it right
        after register/edit); otherwise the alias's listed deployment is used. The gateway
        serves ``/model/info`` from per-replica memory, so a just-registered alias can be
        missing from one replica's list for a moment — the lookup retries briefly.

        Returns ``{"probe": ProbeReport.as_dict(), "recorded": bool}`` for chat models,
        ``{}`` for embeddings. ``recorded`` is False when the record could not be written
        (a config-defined deployment, an unknown id, or a failed PATCH): the probe's verdict
        still stands, but every reader will treat the deployment as unprobed.
        """
        from ringier_a2a_sdk.embeddings import _DEFAULT_DIMENSION, profile_for
        from ringier_a2a_sdk.model_capabilities import PROBED_AT, probe_model

        # The pinned deployment's own model_info when the caller has the id (an edit can leave
        # the old deployment listed under the alias for a moment); the alias's otherwise.
        model = (await self._get_model_by_id_with_retry(model_id)) if model_id else None
        if model is None:
            if model_id:
                logger.warning("[probe] %s: pinned deployment %s not listed; falling back to the alias", model_name, model_id)
            model = await self._get_model_with_retry(model_name)
        info = (model or {}).get("model_info") or {}
        mode = info.get("mode", "chat")
        if mode == "embedding":
            litellm_model = ((model or {}).get("litellm_params") or {}).get("model")
            provider = info.get("litellm_provider")
            body: dict = {"model": model_name, "input": ["ping"]}
            if profile_for(litellm_model, provider).send_dimensions:
                body["dimensions"] = _DEFAULT_DIMENSION
            await self._request("POST", "/v1/embeddings", json=body, timeout=30.0, expose_error=True)
            return {}

        report = await probe_model(model_name, self._probe_call, supports_reasoning=bool(info.get("supports_reasoning")))
        if report.rejected:
            reasons = "; ".join(f"{r.shape}: {r.error}" for r in report.rejected)
            raise ModelGatewayError(f"the model rejects request shapes every agent turn sends — {reasons}")
        if report.inconclusive_unavoidable:
            reasons = "; ".join(f"{r.shape}: {r.error}" for r in report.inconclusive_unavoidable)
            raise ModelGatewayError(f"inconclusive — the probe could not reach the model; re-run the test ({reasons})")

        target_id = model_id or info.get("id")
        recorded = False
        # Only a DB deployment can be written (LiteLLM rejects /model/update on config-defined
        # ones) — judged on the looked-up deployment, not on whether an id was passed.
        target_is_db = bool(info.get("db_model")) if info.get("id") == target_id else True
        if target_id and target_is_db:
            try:
                # Measured keys overwrite; a shape that was attempted but inconclusive keeps
                # whatever was recorded before (noise must not erase knowledge, ADR-0015); a
                # shape not attempted at all (thinking replay on a model no longer declared to
                # think) is dropped. The stored record comes from the target deployment itself.
                src = model if info.get("id") == target_id else await self._get_model_by_id_with_retry(target_id)
                if src is None and report.inconclusive:
                    raise ModelGatewayError("stored record unreadable; not overwriting it with a partial probe")
                prior = dict(capabilities_of((src or {}).get("model_info")))
                kept = {k: v for k, v in prior.items() if k in _keys_for_shapes(r.shape for r in report.inconclusive)}
                await self.record_capabilities(
                    target_id, {**kept, **report.capabilities, PROBED_AT: _utc_now_iso()}
                )
                recorded = True
            except ModelGatewayError as e:
                # The verdict is sound; only the write failed. A passing model must not be
                # reported as failed (the console would roll its registration back).
                logger.warning("[probe] %s: capabilities not recorded on %s: %s", model_name, target_id, e)
        else:
            # Config-defined deployments can't be updated through the management API
            # (LiteLLM rejects /model/update on them); the report still reaches the admin.
            logger.info("[probe] %s has no DB deployment id; capabilities not recorded: %s", model_name, report.capabilities)
        return {"probe": report.as_dict(), "recorded": recorded}

    async def _get_model_with_retry(self, model_name: str, attempts: int = 4, delay: float = 0.75) -> dict | None:
        """``get_model`` with a short retry for a deployment registered a moment ago."""
        return await self._retry_lookup(lambda: self.get_model(model_name), attempts, delay)

    async def _get_model_by_id_with_retry(self, model_id: str, attempts: int = 4, delay: float = 0.75) -> dict | None:
        return await self._retry_lookup(lambda: self.get_model_by_id(model_id), attempts, delay)

    async def _retry_lookup(self, lookup, attempts: int, delay: float) -> dict | None:
        for i in range(attempts):
            model = await lookup()
            if model is not None:
                return model
            if i + 1 < attempts:
                self._invalidate_list_cache()
                await asyncio.sleep(delay)
        return None

    async def _probe_call(self, body: dict) -> object:
        """One probe request on the inference API. JSON for a plain body, the raw SSE text for
        a streaming one (``probe_model`` assembles it). Provider errors surface as
        ``ProbeCallError`` with the provider's reason — safe, the body carries no credentials."""
        from ringier_a2a_sdk.model_capabilities import ProbeCallError

        try:
            client = self._get_client()
            resp = await client.request(
                "POST", f"{self._base_url}/v1/chat/completions", headers=self._headers(), json=body, timeout=60.0
            )
            resp.raise_for_status()
            return resp.text if body.get("stream") else resp.json()
        except httpx.HTTPStatusError as e:
            detail = _provider_error_detail(e.response) or f"gateway returned {e.response.status_code}"
            raise ProbeCallError(detail, status=e.response.status_code) from e
        except httpx.HTTPError as e:
            raise ProbeCallError(f"gateway unreachable: {type(e).__name__}") from e

    async def record_capabilities(self, model_id: str, capabilities: dict) -> None:
        """Store the probe's flags under ``model_info.nannos_capabilities`` on a deployment.

        ``PATCH /model/{id}/update`` merges ``model_info`` (stored ∪ patch) on the proxy
        version the gateway pins, so the deployment's other keys survive and its id is kept —
        unlike ``update_model``, which re-registers. The router picks the change up on its
        next DB reload, so a flag is live within the proxy's reload interval, not instantly."""
        await self._request(
            "PATCH", f"/model/{model_id}/update", json={"model_info": {"id": model_id, CAPABILITIES_KEY: capabilities}}
        )
        self._invalidate_list_cache()
