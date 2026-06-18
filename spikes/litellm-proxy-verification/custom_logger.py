"""Proxy-side capture for the gateway spike (Checks 3 & 4). Throwaway.

LiteLLM loads `proxy_handler_instance` (see config.yaml `callbacks:`). On every
successful call we append one JSON line to SPIKE_CAPTURE_PATH with the *native*
usage breakdown, the real provider/model, the computed response_cost, and the
spend_logs_metadata the client forwarded. Tests on the host read that file.

This is the thing ADR-0002 hinges on: the proxy sees cache_creation / cache_read
/ reasoning tokens and the real provider BEFORE OpenAI-format normalization that
the client response goes through.
"""

import asyncio
import json
import os

from litellm.integrations.custom_logger import CustomLogger

CAPTURE_PATH = os.environ.get("SPIKE_CAPTURE_PATH", "/captured/events.jsonl")
_lock = asyncio.Lock()


def _as_dict(obj):
    """Best-effort dict view of a pydantic/obj usage payload."""
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


def _extract(kwargs, response_obj):
    litellm_params = kwargs.get("litellm_params") or {}
    metadata = litellm_params.get("metadata") or {}

    usage = _as_dict(getattr(response_obj, "usage", None))
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}

    # Cache tokens appear in two shapes depending on normalization:
    #  - Anthropic/Bedrock native: usage.cache_creation_input_tokens / cache_read_input_tokens
    #  - OpenAI-normalized: usage.prompt_tokens_details.cached_tokens (read only; no creation concept)
    cache_creation = usage.get("cache_creation_input_tokens") or usage.get("_cache_creation_input_tokens")
    cache_read = (
        usage.get("cache_read_input_tokens")
        or usage.get("_cache_read_input_tokens")
        or prompt_details.get("cached_tokens")
    )
    reasoning = completion_details.get("reasoning_tokens")

    return {
        "model_requested": kwargs.get("model"),
        "model_backend": litellm_params.get("model"),
        "provider": kwargs.get("custom_llm_provider") or litellm_params.get("custom_llm_provider"),
        "response_cost": kwargs.get("response_cost"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "reasoning_tokens": reasoning,
        "spend_logs_metadata": metadata.get("spend_logs_metadata"),
        "raw_usage": usage,
        "raw_metadata": metadata,  # debugging: see where attribution actually lands
    }


class SpikeLogger(CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        record = _extract(kwargs, response_obj)
        line = json.dumps(record, default=str)
        async with _lock:
            with open(CAPTURE_PATH, "a") as f:
                f.write(line + "\n")

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        # Sync fallback (non-streaming sync path); appends without the async lock.
        record = _extract(kwargs, response_obj)
        with open(CAPTURE_PATH, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")


proxy_handler_instance = SpikeLogger()
