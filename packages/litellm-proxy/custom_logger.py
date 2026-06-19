"""Proxy-side usage/cost capture for the Nannos Model Gateway (ADR-0002).

On every successful LLM call the proxy sees the *native* provider usage — base /
cache_creation / cache_read / reasoning tokens and the real provider+model — before
OpenAI-format normalization. We map that to Nannos billing units (the same keys the
in-app CostTrackingCallback used, so existing Rate Cards match) and POST it to
console-backend's existing ingestion endpoint, carrying the attribution the app
forwarded as `spend_logs_metadata`.

Validated end-to-end in the spike (SPIKE-FINDINGS.md, checks 3 & 4).

Phase status:
  - Extraction + billing-unit mapping + per-event POST: implemented.
  - Service-to-service auth token (OIDC client-credentials, like console-backend's
    orchestrator_cache): TODO(phase-2) — currently uses CONSOLE_BACKEND_TOKEN if set.
"""

import logging
import os

import httpx
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("nannos.litellm.custom_logger")

CONSOLE_BACKEND_URL = os.environ.get("CONSOLE_BACKEND_URL", "").rstrip("/")
# Shared service secret (ADR-0005 style): the gateway-only ingestion route on
# console-backend accepts this bearer and trusts each record's user_sub.
GATEWAY_INGEST_TOKEN = os.environ.get("GATEWAY_INGEST_TOKEN", "")
_INGEST_PATH = "/api/v1/usage/gateway-batch-log"
_HTTP_TIMEOUT = 5.0


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    if isinstance(obj, dict):
        return obj
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")} if hasattr(obj, "__dict__") else {}


def _billing_unit_breakdown(usage: dict) -> dict[str, int]:
    """Map usage → Nannos billing units (same keys as the in-app callback).

    Mirrors LiteLLM's own cost_calculator (`generic_cost_per_token` in
    litellm/litellm_core_utils/llm_cost_calc/utils.py): partition input into three priced
    buckets — full-price base, cache-read (discounted), cache-creation (premium) — that sum
    to the billed input. reasoning is included in completion_tokens.

    The token counts arrive in two shapes and we must handle both without double-counting:
      - LiteLLM-normalized (e.g. Anthropic via calculate_usage): prompt_tokens is
        cache-INCLUSIVE (= base + cache_creation + cache_read); the true non-cache base is
        exposed as prompt_tokens_details.text_tokens, and cache_read/cache_creation are
        mirrored both top-level and under prompt_tokens_details.
      - Native additive (Bedrock-style): cache_read_input_tokens / cache_creation_input_tokens
        are top-level only and NOT part of prompt_tokens, so prompt_tokens is already the base.

    Discriminator: the inclusive portions are exactly the ones reported under
    prompt_tokens_details; top-level-only cache tokens are additive and must NOT be subtracted.
    """
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}

    total_input = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    total_output = usage.get("completion_tokens") or usage.get("output_tokens") or 0

    # cache_read / cache_creation for BILLING: prefer the top-level provider field, fall back
    # to the prompt_tokens_details mirror (LiteLLM-normalized sets both to the same value).
    inclusive_cache_read = prompt_details.get("cached_tokens") or 0
    inclusive_cache_creation = prompt_details.get("cache_creation_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or inclusive_cache_read or 0
    cache_creation = usage.get("cache_creation_input_tokens") or inclusive_cache_creation or 0
    reasoning = completion_details.get("reasoning_tokens") or 0

    # Base (full-price) input. Trust text_tokens when present — it is LiteLLM's authoritative
    # non-cache base (and avoids guessing). Otherwise reconstruct it by subtracting ONLY the
    # inclusive (details-reported) cache portions, since additive top-level tokens were never
    # part of total_input. This fixes the prior bug where cache_creation was billed twice on
    # normalized Anthropic usage (folded into base AND charged as cache_creation).
    text_tokens = prompt_details.get("text_tokens")
    if text_tokens:
        base_input = text_tokens
    else:
        base_input = total_input - inclusive_cache_read - inclusive_cache_creation
        if base_input < 0:  # defensive: never under-bill on an unexpected shape
            base_input = total_input

    breakdown: dict[str, int] = {}
    if base_input > 0:
        breakdown["base_input_tokens"] = base_input
    if cache_creation > 0:
        breakdown["cache_creation_input_tokens"] = cache_creation
    if cache_read > 0:
        breakdown["cache_read_input_tokens"] = cache_read

    base_output = total_output - (reasoning if reasoning else 0)
    if base_output > 0:
        breakdown["base_output_tokens"] = base_output
    if reasoning > 0:
        breakdown["reasoning_output_tokens"] = reasoning

    return {k: v for k, v in breakdown.items() if v > 0}


def _estimate_text_token_units(kwargs: dict) -> int:
    """Fallback ~4-chars/token estimate of an embedding request's text input length.

    Some embedding providers (notably Vertex/Gemini) report 0 tokens in usage, which would
    otherwise bill the call $0. When that happens we re-apply the pre-gateway in-app
    heuristic (len(text) // 4) so text embeddings still carry a cost. Image/data-URI parts
    are excluded — they're billed separately via `input_images`.
    """
    raw = kwargs.get("input")
    if raw is None:
        return 0
    items = raw if isinstance(raw, list) else [raw]
    chars = 0
    for item in items:
        parts = item if isinstance(item, list) else [item]
        for part in parts:
            if isinstance(part, str) and not part.startswith(("data:", "gs://")):
                chars += len(part)
    return chars // 4


def _count_image_inputs(kwargs: dict) -> int:
    """Count image inputs in an embedding request.

    Multimodal (text+image) embeddings report 0 tokens on Vertex, so token-based
    billing misses them — we bill each image explicitly via the `input_images` unit.
    Images arrive as data-URI / gs:// strings (possibly nested in fused-input lists)
    or as dicts.
    """
    raw = kwargs.get("input")
    if raw is None:
        return 0
    items = raw if isinstance(raw, list) else [raw]
    count = 0
    for item in items:
        parts = item if isinstance(item, list) else [item]
        for part in parts:
            if isinstance(part, str) and part.startswith(("data:image", "gs://")):
                count += 1
            elif isinstance(part, dict) and ("image" in part or part.get("type") == "image"):
                count += 1
    return count


def _build_record(kwargs: dict, response_obj) -> dict | None:
    litellm_params = kwargs.get("litellm_params") or {}
    metadata = litellm_params.get("metadata") or {}
    attribution = metadata.get("spend_logs_metadata") or {}

    user_sub = attribution.get("user_sub")
    if not user_sub:
        # No attribution → can't bill it to anyone; skip (matches in-app callback).
        logger.info("[cost] no user_sub in spend_logs_metadata; skipping")
        return None

    # Rate cards key on the public alias (the model_group the caller requested), not the
    # resolved deployment id that kwargs["model"] holds after routing (e.g.
    # "bedrock/anthropic.claude-..."); using the deployment id risks a rate-card miss → $0.
    model_name = metadata.get("model_group") or kwargs.get("model")

    usage = _as_dict(getattr(response_obj, "usage", None))
    breakdown = _billing_unit_breakdown(usage)
    # Multimodal embeddings report 0 tokens (Vertex), so bill images explicitly.
    images = _count_image_inputs(kwargs)
    if images:
        breakdown["input_images"] = breakdown.get("input_images", 0) + images
    # Embedding calls whose provider reported 0 text tokens (Vertex/Gemini): estimate text
    # tokens from input length so they aren't billed $0 (matches the `input_text_tokens`
    # rate-card unit). Only when no token-based input unit was already captured.
    if kwargs.get("input") is not None and "base_input_tokens" not in breakdown:
        estimated = _estimate_text_token_units(kwargs)
        if estimated:
            breakdown["input_text_tokens"] = breakdown.get("input_text_tokens", 0) + estimated
    if not breakdown:
        logger.warning("[cost] empty billing breakdown for model=%s", model_name)
        return None

    return {
        "user_sub": user_sub,
        "provider": kwargs.get("custom_llm_provider") or litellm_params.get("custom_llm_provider"),
        "model_name": model_name,
        "billing_unit_breakdown": breakdown,
        "conversation_id": attribution.get("conversation_id"),
        "sub_agent_id": attribution.get("sub_agent_id"),
        "scheduled_job_id": attribution.get("scheduled_job_id"),
        "sub_agent_config_version_id": attribution.get("sub_agent_config_version_id"),
        "catalog_id": attribution.get("catalog_id"),
    }


class NannosCostLogger(CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            record = _build_record(kwargs, response_obj)
            if record is None:
                return
            if not CONSOLE_BACKEND_URL:
                logger.info("[cost] (no CONSOLE_BACKEND_URL) %s", record)
                return
            headers = {"Content-Type": "application/json"}
            if GATEWAY_INGEST_TOKEN:
                headers["Authorization"] = f"Bearer {GATEWAY_INGEST_TOKEN}"
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    f"{CONSOLE_BACKEND_URL}{_INGEST_PATH}",
                    json={"logs": [record]},
                    headers=headers,
                )
                resp.raise_for_status()
        except Exception as e:  # never break the LLM call on a logging failure
            logger.error("[cost] failed to report usage: %s", e, exc_info=True)


proxy_handler_instance = NannosCostLogger()
