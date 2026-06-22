"""Model factory — creates LangChain clients pointed at the Nannos Model Gateway.

All LLM traffic routes through the LiteLLM proxy. The **gateway is the
single source of truth** for which models exist (runtime registration) — the app
keeps NO static model registry. `create_model` builds one OpenAI-compatible
`ChatOpenAI` per alias; validity comes from the live gateway list; extended thinking
is the unified `reasoning_effort`, always sent (the gateway drops it for non-reasoning
models via `drop_params`). The only per-model check is the non-portable `minimal` tier
(see `get_reasoning_effort`), grounded in the gateway's own capability flags.
"""

import json
import logging
import os
import threading
import time
import urllib.request

from langchain_core.language_models import BaseChatModel

from agent_common.models.base import ModelType, ThinkingLevel

# The gateway URL/key resolvers live in the SDK (the lowest shared layer) so the chat path
# (here) and the embeddings path (ringier_a2a_sdk.embeddings) can never drift — notably the
# virtual-key default, which used to be copy-pasted and silently 401'd a path when missed.
from ringier_a2a_sdk.utils.gateway import gateway_api_key as _gateway_api_key
from ringier_a2a_sdk.utils.gateway import gateway_base_url as _gateway_base_url

logger = logging.getLogger(__name__)


class NoDefaultModelError(RuntimeError):
    """Raised when a runtime caller needs the fleet default chat model but none is configured.

    Signals a configuration gap (no "chat" default set in the console), not a transient error
    — retrying won't help until an admin sets the default. See require_default_model()."""


def _has_aws_credentials() -> bool:
    """Whether AWS credentials are available (env, profile, or instance role).

    Chat models no longer need this (they go through the gateway). It remains only
    for the direct-Bedrock embeddings path, which migrates to the gateway in Phase 5.
    """
    try:
        import botocore.session

        return botocore.session.get_session().get_credentials() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Gateway client
# ---------------------------------------------------------------------------


def assert_gateway_configured() -> None:
    """Fail fast at startup when the gateway URL is missing.

    The gateway is the sole path for LLM traffic, so a service that needs
    models should call this in its startup hook to surface the misconfiguration loudly
    at boot rather than as an opaque per-request failure deep in the graph."""
    _gateway_base_url()


# Reasoning-effort tiers that are NOT portable across providers: OpenAI-family models
# accept them as distinct tiers, but Anthropic/Bedrock/Vertex reject them as invalid values.
# Each maps to the nearest portable tier it floors to when the model doesn't declare support.
# Gated on the gateway model_info ``supports_<tier>_reasoning_effort`` flag — the same flags
# console-backend's ``thinking_levels_for`` uses to decide which levels to offer, so the UI and
# this runtime mapping never disagree.
_NON_PORTABLE_EFFORT: dict[str, str] = {"minimal": "low", "xhigh": "high"}


def get_reasoning_effort(
    thinking_level: ThinkingLevel | None, model_type: ModelType | None = None
) -> str | None:
    """Map the app `thinking_level` to LiteLLM's `reasoning_effort`.

    low/medium/high are accepted by every reasoning provider and pass through. The
    non-portable tiers (`minimal`, `xhigh`) are preserved only when the gateway model_info
    declares the matching ``supports_<tier>_reasoning_effort`` capability; otherwise each
    floors to its nearest portable tier (`minimal`→`low`, `xhigh`→`high`), including when the
    model is unknown or the gateway snapshot is cold. The gateway drops the param entirely for
    non-reasoning models (`drop_params`)."""
    if not thinking_level:
        return None
    value = thinking_level.value if isinstance(thinking_level, ThinkingLevel) else str(thinking_level)
    floor = _NON_PORTABLE_EFFORT.get(value)
    if floor is not None:
        info = _gateway_models().get(model_type) if model_type else None
        if not (info and info.get(f"supports_{value}_reasoning_effort")):
            return floor
    return value


def create_model(
    model_type: ModelType,
    thinking_level: ThinkingLevel | None = None,
    callbacks: list | None = None,
    streaming: bool = True,
) -> BaseChatModel:
    """Create a gateway-backed chat model for the given alias.

    `thinking_level` becomes `reasoning_effort` and is always forwarded; the gateway
    drops it for models that don't support reasoning (`drop_params`), so no per-model
    capability check is needed here.
    """
    from langchain_openai import ChatOpenAI

    from ringier_a2a_sdk.cost_tracking.attribution import build_attribution_http_client

    model_type = resolve_chat_model(model_type)
    # The proxy CostLogger is the single source of cost for all gateway traffic.
    # Drop any in-app CostTrackingCallback some call sites still pass, or the
    # call is double-counted — once proxy-side (correct provider) and once in-app (which
    # sees the OpenAI-compatible client and mislabels the provider as "openai").
    if callbacks:
        callbacks = [cb for cb in callbacks if type(cb).__name__ != "CostTrackingCallback"] or None

    model_kwargs: dict = {}
    effort = get_reasoning_effort(thinking_level, model_type)
    if effort:
        model_kwargs["reasoning_effort"] = effort

    logger.info("Creating gateway model alias=%s thinking=%s streaming=%s", model_type, effort, streaming)
    return ChatOpenAI(
        base_url=_gateway_base_url(),
        api_key=_gateway_api_key(),
        model=model_type,
        streaming=streaming,
        stream_usage=True,  # usage in the final chunk so cost callbacks see usage_metadata
        callbacks=callbacks,
        http_async_client=build_attribution_http_client(),  # per-request attribution
        model_kwargs=model_kwargs,
    )


class EmbeddingModelNotConfigured(RuntimeError):
    """Raised when no embedding model is available — neither requested nor a default.

    Embedding-dependent features should catch this and disable gracefully (an admin must
    set a default embedding model in the console first)."""


# Single source of truth for the embedding vector dimension. The pgvector document-store
# index is created with a fixed `dims`, so the embeddings we produce MUST match it or
# inserts/similarity-search break. We pin the request dimension here (Matryoshka-capable
# models truncate to it) and the stores read the SAME value for their index `dims`.
# Defaults to 1024 (the historical Titan/Gemini dimension); override only in lockstep with
# a re-index, since changing it invalidates existing vectors.
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "1024"))


def get_embedding_dimension() -> int:
    """The embedding vector dimension to request and to size the pgvector index with.

    Stores MUST use this for their index `dims` so the index and the produced vectors agree
    (see create_embeddings, which forwards it as the `dimensions` request param)."""
    return EMBEDDING_DIMENSION


def create_embeddings(model_type: str | None = None, multimodal: bool = False):
    """Create a gateway-backed text embeddings client.

    Cost is captured proxy-side via the attribution http client. When
    `model_type` is omitted, the configured **default** embedding model is used (the
    "use the default when set" behavior); if no default is set, raises
    EmbeddingModelNotConfigured so callers can disable the feature gracefully. An explicit
    `model_type` is honored as-is (a retired alias still degrades to the default).

    The output `dimensions` is pinned to get_embedding_dimension() so the produced vectors
    match the pgvector index the document store is created with — otherwise a default model
    whose native dimension differs from the index (e.g. 3072 vs 1024) breaks inserts/search.
    """
    from langchain_openai import OpenAIEmbeddings

    from ringier_a2a_sdk.cost_tracking.attribution import build_attribution_http_client

    if model_type is None:
        model_type = get_default_embedding_model(multimodal)
        if not model_type:
            raise EmbeddingModelNotConfigured(
                "No default embedding model is configured — set one in the console (Admin → Model Gateway)."
            )
    else:
        model_type = resolve_embedding_model(model_type, multimodal=multimodal)
    return OpenAIEmbeddings(
        base_url=_gateway_base_url(),
        api_key=_gateway_api_key(),
        model=model_type,
        dimensions=get_embedding_dimension(),  # match the store's pgvector index dims
        http_async_client=build_attribution_http_client(),
        check_embedding_ctx_length=False,  # don't run tiktoken against a non-OpenAI model
    )


# ---------------------------------------------------------------------------
# Live model registry (from the gateway; cached). No static config.
# ---------------------------------------------------------------------------

# Cached snapshot of the gateway registry: {model_name: model_info}. model_info
# carries capabilities (input_modes, supports_reasoning, …) set at registration.
# Fetched with the app's virtual key (no master key needed). ts starts
# far in the past so the first call fetches; every path updates ts so failures are
# cached too (no per-call refetch storm).
# These caches are read from sync `create_model`/resolution helpers that run inside the
# apps' async event loops. To avoid blocking the loop on network I/O, the fetch runs on
# a daemon thread and callers get the cached snapshot immediately; we only ever block on
# the very first fetch (cold start, typically at app startup) so there's data to serve.
_COLD = -1e9
_COLD_WAIT = 3.0  # cap a cold waiter's block; fetch() itself times out at 2s


def _refresh_if_stale(cache: dict, key: str, ttl: float, cond: threading.Condition, fetch) -> None:
    if time.monotonic() - cache["ts"] < ttl:
        return  # fast path: fresh enough, no lock

    with cond:
        # Re-check under the lock — another thread may have refreshed or started since.
        if time.monotonic() - cache["ts"] < ttl:
            return
        cold = cache["ts"] == _COLD
        if cache["inflight"]:
            if not cold:
                return  # warm: a background refresh is running → serve the stale snapshot
            # Cold: someone else is doing the first population. Block until it lands so we
            # never hand back an empty registry. _run() always notifies (success or failure),
            # so the only way to hit the timeout is a genuinely stuck owner.
            cond.wait_for(lambda: not (cache["inflight"] and cache["ts"] == _COLD), timeout=_COLD_WAIT)
            return
        cache["inflight"] = True  # we own this refresh

    def _run():
        try:
            cache[key] = fetch()
            cache["last_error"] = None
        except Exception as e:
            cache["last_error"] = e
            logger.debug("Background refresh of '%s' failed: %s", key, e)
        finally:
            with cond:
                cache["ts"] = time.monotonic()  # set even on failure → back off a full TTL
                cache["inflight"] = False
                cond.notify_all()  # release any cold waiters

    if cold:
        _run()  # block this owner until populated (no stale snapshot yet)
    else:
        threading.Thread(target=_run, daemon=True, name=f"refresh-{key}").start()


_GW_CACHE: dict = {"ts": _COLD, "models": {}, "inflight": False, "last_error": None}
_GW_TTL = 60.0
_GW_LOCK = threading.Condition()


def _fetch_gateway_models() -> dict[str, dict]:
    # _gateway_base_url() raises when LLM_GATEWAY_URL is unset — a misconfiguration, NOT an
    # empty registry. The error is recorded in `last_error` (models_known_empty() then
    # returns False so the orchestrator won't tell the user "no models registered").
    req = urllib.request.Request(
        _gateway_base_url() + "/v1/model/info",
        headers={"Authorization": f"Bearer {_gateway_api_key()}"},
    )
    with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 (internal cluster URL)
        data = json.loads(resp.read()).get("data", [])
    return {m["model_name"]: (m.get("model_info") or {}) for m in data if m.get("model_name")}


def _gateway_models() -> dict[str, dict]:
    """{model_name: model_info} from the gateway /v1/model/info (cached, refreshed
    off-thread). model_info carries capabilities set at registration; fetched with the
    app's virtual key (no master key needed)."""
    _refresh_if_stale(_GW_CACHE, "models", _GW_TTL, _GW_LOCK, _fetch_gateway_models)
    return _GW_CACHE["models"]


# Defaults live in console-backend (not the gateway): LiteLLM's /model/update can't
# persist a custom flag, so console-backend is the authoritative, runtime-editable store
# and exposes them on an unauthenticated in-cluster endpoint.
_DEFAULTS_CACHE: dict = {"ts": _COLD, "defaults": {}, "inflight": False, "last_error": None}
_DEFAULTS_TTL = 60.0
_DEFAULTS_LOCK = threading.Condition()


def _fetch_model_defaults() -> dict[str, str]:
    base = os.getenv("CONSOLE_BACKEND_URL")
    if not base:
        return _DEFAULTS_CACHE["defaults"]  # nothing to fetch; keep last-known
    req = urllib.request.Request(base.rstrip("/") + "/api/v1/models/defaults")
    with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 (internal cluster URL)
        return json.loads(resp.read()) or {}


def _model_defaults() -> dict[str, str]:
    """{role: default_alias} from console-backend /api/v1/models/defaults (cached,
    refreshed off-thread)."""
    _refresh_if_stale(_DEFAULTS_CACHE, "defaults", _DEFAULTS_TTL, _DEFAULTS_LOCK, _fetch_model_defaults)
    return _DEFAULTS_CACHE["defaults"]


def _default_alias_for(role: str) -> ModelType | None:
    """The alias the console has set as default for a role (chat/embedding/
    multimodal_embedding), so a retired alias degrades gracefully."""
    return _model_defaults().get(role)  # type: ignore[return-value]


def get_default_embedding_model(multimodal: bool = False) -> ModelType | None:
    """The configured default embedding alias, or None when none is set.

    Multimodal prefers the multimodal-embedding default, then the text default. When this
    returns None, embedding-dependent features should disable gracefully rather than fail
    (an admin must set a default embedding model in the console first)."""
    if multimodal:
        return _default_alias_for("multimodal_embedding") or _default_alias_for("embedding")
    return _default_alias_for("embedding")


def is_embeddings_configured(multimodal: bool = False) -> bool:
    """Whether a default embedding model is set (gate embedding-dependent features on this)."""
    return get_default_embedding_model(multimodal) is not None


def embedding_default_known_absent(multimodal: bool = False) -> bool:
    """True only when the model-defaults endpoint was queried SUCCESSFULLY and still has no
    embedding default — a genuine 'an admin must set one' state.

    The counterpart to models_known_empty() for the defaults cache: it lets callers tell a
    permanently-unconfigured embedding default (disable the feature) apart from a cold or
    failed fetch (retry later) — without it, both look identical (get_default_embedding_model
    returns None), which is what makes a transient cold-start latch the document store off."""
    absent = get_default_embedding_model(multimodal) is None  # triggers a defaults fetch
    return absent and _DEFAULTS_CACHE.get("last_error") is None


def _resolve_alias(model_type: str, roles: tuple[str, ...], kind: str) -> str:
    """Map a requested alias to one that's actually registered, degrading to the gateway's
    default for the first of ``roles`` that has one when the requested alias is retired.

    When the gateway list can't be read we pass through unchanged — the gateway is the
    authority and will 400 on a genuinely unknown alias."""
    models = _gateway_models()
    if not models or model_type in models:
        return model_type
    for role in roles:
        default = _default_alias_for(role)
        if default and default != model_type:
            logger.warning(
                "%s model '%s' not registered on the gateway; falling back to default '%s' (role=%s)",
                kind,
                model_type,
                default,
                role,
            )
            return default
    logger.warning("%s model '%s' not registered and no gateway default set; passing through", kind, model_type)
    return model_type


def resolve_chat_model(model_type: ModelType) -> ModelType:
    """Map a requested chat alias to one that's actually registered, degrading to the
    gateway's default-for-chat model when the requested one has been retired."""
    return _resolve_alias(model_type, ("chat",), "Chat")


def resolve_embedding_model(model_type: str, multimodal: bool = False) -> str:
    """Embedding counterpart to resolve_chat_model. A multimodal request prefers the
    default-for-multimodal_embedding model, then the plain embedding default."""
    roles = ("multimodal_embedding", "embedding") if multimodal else ("embedding",)
    return _resolve_alias(model_type, roles, "Embedding")


def get_available_models() -> list[ModelType]:
    """Models registered on the gateway (live)."""
    return list(_gateway_models().keys())  # type: ignore[return-value]


def models_known_empty() -> bool:
    """True only when the gateway was queried successfully and returned no models.

    Distinguishes a genuinely empty registry (guide the admin to register one) from a
    transient/cold-start fetch failure (don't block — the gateway is the authority).
    On a failed or not-yet-completed fetch this returns False so callers fail open."""
    models = _gateway_models()
    return _GW_CACHE.get("last_error") is None and not models


def is_valid_model(model_name: str) -> bool:
    """Valid if registered on the gateway. When the gateway list can't be read, don't
    reject — the gateway is the authority and will 400 on a genuinely unknown alias."""
    models = _gateway_models()
    return (model_name in models) if models else True


def get_model_input_capabilities(model_type: ModelType) -> list[str]:
    """Content types a model accepts, from the gateway model_info (set at registration).

    This is the orchestrator's source of truth for what payloads it can send to a
    (dynamic) sub-agent. Falls back to text+image only if the gateway snapshot or the
    model's input_modes is unavailable.
    """
    info = _gateway_models().get(model_type) or {}
    modes = info.get("input_modes")
    return list(modes) if modes else ["text", "image"]


def get_model_provider(model_type: ModelType) -> str:
    """The LiteLLM provider family for a model, from the gateway model_info
    (e.g. ``'bedrock_converse'``, ``'openai'``, ``'azure'``, ``'vertex_ai'``); ``''`` when
    the model is unknown or the gateway snapshot is unavailable.

    Gateway-native replacement for branching on hardcoded alias strings: the
    request strategy a model needs (tool-based vs native structured output, Gemini's
    text-embedded JSON, built-in tools) follows from its provider/capabilities, not from
    its name — so a renamed/re-registered alias (e.g. ``claude-sonnet-4-6`` vs
    ``claude-sonnet-4.6``) can't silently route into the wrong branch.
    """
    info = _gateway_models().get(model_type) or {}
    return info.get("litellm_provider") or ""


def is_gemini_model(model_type: ModelType) -> bool:
    """Whether a model is Google Gemini — and thus accepts Google's server-side built-in
    tools (google_search, code_execution) and emits text-embedded structured JSON.

    Interim heuristic: the gateway exposes no built-in-tools capability flag yet, and these
    tools are Google-product-specific, so some Google-specificity is unavoidable. Gemini is
    served via the ``gemini`` provider (AI Studio) or ``vertex_ai`` (Vertex). Crucially we do
    NOT treat all of ``vertex_ai`` as Gemini — Vertex also hosts Claude/Llama, which must not
    be bound Google built-in tools (the gateway would 4xx). For Vertex we additionally require
    the alias to name gemini. Fails safe: a mis-detected Gemini merely misses built-in tools
    rather than binding wrong ones. Replace with a gateway capability flag when one lands.
    """
    provider = get_model_provider(model_type)
    if provider == "gemini":
        return True
    if provider == "vertex_ai":
        return "gemini" in (model_type or "").lower()
    return False


def get_default_model() -> ModelType | None:
    """Fleet default chat model: the console's default-for-chat alias, or ``None`` when none
    is set.

    The gateway / console model_defaults store ("chat" role) is the single source of truth —
    there is no env var or hardcoded alias fallback (models are registered at runtime, so the
    app keeps no static default). Read-only callers (badging, logging, indexing degradation)
    tolerate ``None``; callers that must actually run a model use require_default_model()."""
    return _default_alias_for("chat")


def require_default_model() -> ModelType:
    """The fleet default chat model, raising NoDefaultModelError when none is configured.

    For runtime callers (agent construction, graph creation, sub-agent invocation) that need
    a concrete model and cannot proceed without one. Per the runtime-registration policy there
    is no fallback alias: an admin must set the "chat" default in the console. Crucially the
    console backend itself must NOT call this — it has to stay reachable with no default set so
    an admin can configure one (use get_default_model() there and tolerate ``None``)."""
    model = get_default_model()
    if model is None:
        raise NoDefaultModelError(
            "No default chat model is configured. An admin must set the 'chat' default in the "
            "console (Models → Defaults) before agents can run."
        )
    return model


def get_available_models_metadata() -> list[dict]:
    """Minimal picker metadata from the live list. Rich capability/label data is
    served by console-backend directly from the gateway `/model/info`; this
    orchestrator-side view is intentionally minimal."""
    default_model = get_default_model()
    return [
        {"value": name, "label": name, "provider": "Model Gateway", "is_default": name == default_model}
        for name in get_available_models()
    ]


def get_default_fast_model() -> ModelType | None:
    """Default model for cheap, low-latency utility LLM calls — file filtering, tool-risk
    scoring, watch-condition evaluation, notification-message generation, etc.

    Runs on the low chat tier (``chat:low``) — the fleet's designated cheap chat model —
    falling back to the standard chat default when no low tier is set. No hardcoded alias and
    no per-task model slot: the gateway + the existing chat tiers are the single source of
    truth, so a retired/renamed alias never silently routes a task to an arbitrary model.
    Returns ``None`` only when no chat default is configured at all; runtime callers that must
    actually run a model pair this with require_default_model()."""
    return _default_alias_for("chat:low") or get_default_model()


def get_default_indexing_model() -> ModelType | None:
    """Model for semantic indexing/chunking (generating context descriptions).

    Indexing is high-volume and cost-sensitive, so it runs on the cheap tier — see
    get_default_fast_model()."""
    return get_default_fast_model()
