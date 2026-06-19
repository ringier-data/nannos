"""Model factory — creates LangChain clients pointed at the Nannos Model Gateway.

All LLM traffic routes through the LiteLLM proxy (ADR-0001). The **gateway is the
single source of truth** for which models exist (runtime registration, Q6) — the app
keeps NO static model registry. `create_model` builds one OpenAI-compatible
`ChatOpenAI` per alias; validity comes from the live gateway list; extended thinking
is the unified `reasoning_effort`, always sent (the gateway drops it for non-reasoning
models via `drop_params`), so the app needs no per-model capability table.
"""

import json
import logging
import os
import threading
import time
import urllib.request

from langchain_core.language_models import BaseChatModel

from agent_common.models.base import ModelType, ThinkingLevel, get_resolved_default_model

logger = logging.getLogger(__name__)


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


def _gateway_base_url() -> str:
    url = os.getenv("LLM_GATEWAY_URL")
    if not url:
        raise RuntimeError(
            "LLM_GATEWAY_URL is not set. The Model Gateway is the sole path for LLM "
            "calls (ADR-0001); point it at the litellm-proxy service."
        )
    return url.rstrip("/")


def assert_gateway_configured() -> None:
    """Fail fast at startup when the gateway URL is missing.

    The gateway is the sole path for LLM traffic (ADR-0001), so a service that needs
    models should call this in its startup hook to surface the misconfiguration loudly
    at boot rather than as an opaque per-request failure deep in the graph."""
    _gateway_base_url()


def get_reasoning_effort(thinking_level: ThinkingLevel | None) -> str | None:
    """Map the app `thinking_level` (minimal/low/medium/high) to LiteLLM's unified
    `reasoning_effort` (ADR-0003). `minimal` has no distinct provider tier, so it floors
    to `low` (Bedrock floors at 1024 anyway); the rest pass through. The gateway drops
    the param for models that don't support a given level (`drop_params`)."""
    if not thinking_level:
        return None
    value = thinking_level.value if isinstance(thinking_level, ThinkingLevel) else str(thinking_level)
    return "low" if value == "minimal" else value


def create_model(
    model_type: ModelType,
    bedrock_region: str | None = None,  # accepted for call-site compatibility; gateway owns region
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

    from agent_common.core.attribution import build_attribution_http_client

    model_type = resolve_chat_model(model_type)
    # The proxy CostLogger is the single source of cost for all gateway traffic
    # (ADR-0002). Drop any in-app CostTrackingCallback some call sites still pass, or the
    # call is double-counted — once proxy-side (correct provider) and once in-app (which
    # sees the OpenAI-compatible client and mislabels the provider as "openai").
    if callbacks:
        callbacks = [cb for cb in callbacks if type(cb).__name__ != "CostTrackingCallback"] or None

    model_kwargs: dict = {}
    effort = get_reasoning_effort(thinking_level)
    if effort:
        model_kwargs["reasoning_effort"] = effort

    logger.info("Creating gateway model alias=%s thinking=%s streaming=%s", model_type, effort, streaming)
    return ChatOpenAI(
        base_url=_gateway_base_url(),
        api_key=os.getenv("LLM_GATEWAY_API_KEY", "sk-nannos-gateway"),
        model=model_type,
        streaming=streaming,
        stream_usage=True,  # usage in the final chunk so cost callbacks see usage_metadata
        callbacks=callbacks,
        http_async_client=build_attribution_http_client(),  # per-request attribution (ADR-0002)
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
    """Create a gateway-backed text embeddings client (ADR-0001).

    Cost is captured proxy-side (ADR-0002) via the attribution http client. When
    `model_type` is omitted, the configured **default** embedding model is used (the
    "use the default when set" behavior); if no default is set, raises
    EmbeddingModelNotConfigured so callers can disable the feature gracefully. An explicit
    `model_type` is honored as-is (a retired alias still degrades to the default).

    The output `dimensions` is pinned to get_embedding_dimension() so the produced vectors
    match the pgvector index the document store is created with — otherwise a default model
    whose native dimension differs from the index (e.g. 3072 vs 1024) breaks inserts/search.
    """
    from langchain_openai import OpenAIEmbeddings

    from agent_common.core.attribution import build_attribution_http_client

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
        api_key=os.getenv("LLM_GATEWAY_API_KEY", "sk-nannos-gateway"),
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
# Fetched with the app's virtual key (no master key needed — ADR-0005). ts starts
# far in the past so the first call fetches; every path updates ts so failures are
# cached too (no per-call refetch storm).
# These caches are read from sync `create_model`/resolution helpers that run inside the
# apps' async event loops. To avoid blocking the loop on network I/O, the fetch runs on
# a daemon thread and callers get the cached snapshot immediately; we only ever block on
# the very first fetch (cold start, typically at app startup) so there's data to serve.
_COLD = -1e9


def _refresh_if_stale(cache: dict, key: str, ttl: float, lock: threading.Lock, fetch) -> None:
    if time.monotonic() - cache["ts"] < ttl:
        return
    cold = cache["ts"] == _COLD
    with lock:
        if cache["inflight"]:
            return
        cache["inflight"] = True

    def _run():
        try:
            cache[key] = fetch()
            cache["last_error"] = None
        except Exception as e:
            cache["last_error"] = e
            logger.debug("Background refresh of '%s' failed: %s", key, e)
        finally:
            cache["ts"] = time.monotonic()  # set even on failure → back off a full TTL
            cache["inflight"] = False

    if cold:
        _run()  # block once so there's something to serve (no stale snapshot yet)
    else:
        threading.Thread(target=_run, daemon=True, name=f"refresh-{key}").start()


_GW_CACHE: dict = {"ts": _COLD, "models": {}, "inflight": False, "last_error": None}
_GW_TTL = 60.0
_GW_LOCK = threading.Lock()


def _fetch_gateway_models() -> dict[str, dict]:
    base = os.getenv("LLM_GATEWAY_URL")
    if not base:
        # An unset gateway URL is a misconfiguration, NOT an empty registry. Raise so the
        # error is recorded in `last_error` (models_known_empty() then returns False and the
        # orchestrator won't tell the user "no models registered"); the real cause surfaces
        # via _gateway_base_url() raising on the next create_model call.
        raise RuntimeError(
            "LLM_GATEWAY_URL is not set. The Model Gateway is the sole path for LLM "
            "calls (ADR-0001); point it at the litellm-proxy service."
        )
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/model/info",
        headers={"Authorization": f"Bearer {os.getenv('LLM_GATEWAY_API_KEY', '')}"},
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
_DEFAULTS_LOCK = threading.Lock()


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
    multimodal_embedding), so a retired alias degrades gracefully (ADR-0001)."""
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


def resolve_chat_model(model_type: ModelType) -> ModelType:
    """Map a requested chat alias to one that's actually registered, degrading to the
    gateway's default-for-chat model when the requested one has been retired.

    When the gateway list can't be read we pass through unchanged — the gateway is the
    authority and will 400 on a genuinely unknown alias."""
    models = _gateway_models()
    if not models or model_type in models:
        return model_type
    default = _default_alias_for("chat")
    if default and default != model_type:
        logger.warning(
            "Chat model '%s' not registered on the gateway; falling back to default '%s'", model_type, default
        )
        return default
    logger.warning("Chat model '%s' not registered and no gateway chat default set; passing through", model_type)
    return model_type


def resolve_embedding_model(model_type: str, multimodal: bool = False) -> str:
    """Embedding counterpart to resolve_chat_model. A multimodal request prefers the
    default-for-multimodal_embedding model, then the plain embedding default."""
    models = _gateway_models()
    if not models or model_type in models:
        return model_type
    roles = ("multimodal_embedding", "embedding") if multimodal else ("embedding",)
    for role in roles:
        default = _default_alias_for(role)
        if default and default != model_type:
            logger.warning(
                "Embedding model '%s' not registered; falling back to default '%s' (role=%s)",
                model_type,
                default,
                role,
            )
            return default
    logger.warning("Embedding model '%s' not registered and no gateway embedding default set; passing through", model_type)
    return model_type


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


def get_default_model() -> ModelType:
    """Fleet default chat model: the gateway's default-for-chat flag if set, else the
    configured DEFAULT_MODEL env (DB-stored default wins, ADR-0001)."""
    return _default_alias_for("chat") or get_resolved_default_model()


def get_available_models_metadata() -> list[dict]:
    """Minimal picker metadata from the live list. Rich capability/label data is
    served by console-backend directly from the gateway `/model/info` (Q6); this
    orchestrator-side view is intentionally minimal."""
    default_model = get_default_model()
    return [
        {"value": name, "label": name, "provider": "Model Gateway", "is_default": name == default_model}
        for name in get_available_models()
    ]


def get_default_indexing_model() -> ModelType:
    """Model for semantic indexing/chunking (generating context descriptions).

    Follows the per-role default convention (ADR-0001), same as chat/embedding: use the
    console-set "indexing" default if present, else degrade to the fleet chat default.
    No hardcoded alias list — the gateway + model_defaults are the single source of truth,
    so a retired/renamed alias never silently routes indexing to an arbitrary (possibly
    expensive) model."""
    return _default_alias_for("indexing") or get_default_model()
