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
    """Map native usage → Nannos billing units (same keys as the in-app callback).

    cache_creation / cache_read are *additive* on Bedrock (not included in
    prompt_tokens); reasoning is included in completion_tokens.
    """
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}

    total_input = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    total_output = usage.get("completion_tokens") or usage.get("output_tokens") or 0

    cache_creation = (
        usage.get("cache_creation_input_tokens")
        or prompt_details.get("cache_creation_tokens")
        or 0
    )
    cache_read = (
        usage.get("cache_read_input_tokens")
        or prompt_details.get("cached_tokens")
        or 0
    )
    reasoning = completion_details.get("reasoning_tokens") or 0

    breakdown: dict[str, int] = {}
    base_input = total_input - (cache_read if cache_read else 0)
    # On providers where cache tokens are additive, prompt_tokens IS the base.
    if cache_read and base_input < 0:
        base_input = total_input
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

    usage = _as_dict(getattr(response_obj, "usage", None))
    breakdown = _billing_unit_breakdown(usage)
    # Multimodal embeddings report 0 tokens (Vertex), so bill images explicitly.
    images = _count_image_inputs(kwargs)
    if images:
        breakdown["input_images"] = breakdown.get("input_images", 0) + images
    if not breakdown:
        logger.warning("[cost] empty billing breakdown for model=%s", kwargs.get("model"))
        return None

    return {
        "user_sub": user_sub,
        "provider": kwargs.get("custom_llm_provider") or litellm_params.get("custom_llm_provider"),
        "model_name": kwargs.get("model"),
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
