"""Proxy-side usage/cost capture for the Nannos Model Gateway.

On every successful LLM call the proxy sees the *native* provider usage — base /
cache_creation / cache_read / reasoning tokens and the real provider+model — before
OpenAI-format normalization. We map that to Nannos billing units (the same keys the
in-app CostTrackingCallback used, so existing Rate Cards match) and POST it to
console-backend's existing ingestion endpoint, carrying the attribution the app
forwarded as `spend_logs_metadata`.

Validated end-to-end against Bedrock: cache/cost fidelity and attribution round-trip.

Phase status:
  - Extraction + billing-unit mapping + per-event POST: implemented.
  - Service-to-service auth token (OIDC client-credentials, like console-backend's
    orchestrator_cache): TODO(phase-2) — currently uses CONSOLE_BACKEND_TOKEN if set.
"""

import asyncio
import json
import logging
import os

import httpx
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("nannos.litellm.custom_logger")

CONSOLE_BACKEND_URL = os.environ.get("CONSOLE_BACKEND_URL", "").rstrip("/")
# Shared service secret: the gateway-only ingestion route on
# console-backend accepts this bearer and trusts each record's user_sub.
GATEWAY_INGEST_TOKEN = os.environ.get("GATEWAY_INGEST_TOKEN", "")
_INGEST_PATH = "/api/v1/usage/gateway-batch-log"
_HTTP_TIMEOUT = 5.0
# Batching: records are enqueued (non-blocking) on the LLM hot path and flushed by a
# background worker over a single shared connection pool. Coalescing is natural — records
# that arrive while a batch is in flight ship together on the next flush.
_FLUSH_MAX_BATCH = 100  # ship at most this many records per POST
_MAX_BUFFER = 10_000  # hard cap so a console-backend outage can't grow memory unbounded
# Transient flush failures (5xx / timeout / connection) are retried with exponential
# backoff; a 4xx (bad token, malformed payload) is never retried — it won't fix itself and
# would wedge the single-worker queue behind a poison batch. Records that still can't be
# delivered are dead-lettered to the error log (the only billing sink is console-backend, so
# a local store would have to reach it too — logs are the backend-independent fallback).
_FLUSH_MAX_RETRIES = 3  # retry attempts after the first, for retryable failures
_FLUSH_BACKOFF_BASE = 0.5  # seconds, doubled per attempt: 0.5, 1.0, 2.0

# One process-wide client (a connection pool to console-backend) reused across all events,
# instead of building+tearing down a pool per LLM call. Bound to the proxy's event loop on
# first use.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _client


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
    # Some providers (embeddings especially) report only `total_tokens` — no prompt/input split.
    # With neither an input field nor any output, the total IS the input, so bill it as real
    # tokens rather than falling through to the char estimate (or $0). Guarded on output being
    # absent so a chat call's total (= input + output) is never miscounted as input.
    if not total_input and not total_output:
        total_input = usage.get("total_tokens") or 0

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

    # Server-side web search (e.g. Gemini grounding): a token-only breakdown misses the per-query
    # search fee LiteLLM prices via model_info.search_context_cost_per_query. The provider reports
    # prompt_tokens_details.web_search_requests when a call grounded; LiteLLM bills ONE grounding
    # request per call regardless of how many underlying searches it ran, so we emit a single
    # `web_search` unit (priced on the rate card) — NOT the web_search_requests count, which would
    # over-bill. Verified against x-litellm-response-cost (1 × $0.014/query) on 2026-06-25.
    if prompt_details.get("web_search_requests"):
        breakdown["web_search"] = 1

    return {k: v for k, v in breakdown.items() if v > 0}


def _estimate_text_token_units(kwargs: dict) -> int:
    """Fallback ~4-chars/token estimate of an embedding request's text input length.

    Some embedding providers (notably Vertex/Gemini) report 0 tokens in usage, which would
    otherwise bill the call $0. When that happens we re-apply the pre-gateway in-app
    heuristic (~len(text) / 4) so text embeddings still carry a cost. Any non-empty text
    rounds UP to at least 1 token (ceil division), so a 1-3 char input isn't floored to 0 and
    billed $0. Image/data-URI parts are excluded — they're billed separately via
    `input_images`. Pre-tokenized input (lists of integer token ids) is counted directly —
    one id is one token — so it isn't billed $0.
    """
    raw = kwargs.get("input")
    if raw is None:
        return 0
    items = raw if isinstance(raw, list) else [raw]
    chars = 0
    token_ids = 0
    for item in items:
        parts = item if isinstance(item, list) else [item]
        for part in parts:
            if isinstance(part, bool):
                continue  # bool is an int subclass; never a token id
            if isinstance(part, str) and not part.startswith(("data:", "gs://")):
                chars += len(part)
            elif isinstance(part, int):
                token_ids += 1
    # Ceil division: any non-empty text bills at least 1 token (a 1-3 char input must not
    # floor to 0 and get billed $0).
    return -(-chars // 4) + token_ids


def _count_image_inputs(kwargs: dict) -> int:
    """Count image inputs in an embedding request.

    Multimodal (text+image) embeddings report 0 tokens on Vertex, so token-based
    billing misses them — we bill each binary part explicitly via the `input_images` unit.
    Binary parts arrive as data-URI / gs:// strings (possibly nested in fused-input lists)
    or as dicts.

    The accepted prefixes mirror the exclusion in _estimate_text_token_units exactly: any
    ``data:``/``gs://`` part the text estimator drops as non-text is billed here instead, so a
    non-image data URI (e.g. ``data:application/pdf``, ``data:text/...``) is never counted by
    neither and billed $0.
    """
    raw = kwargs.get("input")
    if raw is None:
        return 0
    items = raw if isinstance(raw, list) else [raw]
    count = 0
    for item in items:
        parts = item if isinstance(item, list) else [item]
        for part in parts:
            if isinstance(part, str) and part.startswith(("data:", "gs://")):
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

    # Provider is required for system rate-card resolution; LiteLLM does not always populate
    # custom_llm_provider on passthrough/routed calls. Fall back to the provider prefix of the
    # resolved deployment id ("bedrock/anthropic.claude-..." → "bedrock") so the record stays
    # priceable instead of landing at $0 and silently under-counting budget spend.
    provider = kwargs.get("custom_llm_provider") or litellm_params.get("custom_llm_provider")
    if not provider:
        deployment_id = kwargs.get("model") or ""
        if "/" in deployment_id:
            provider = deployment_id.split("/", 1)[0]
    if not provider:
        logger.warning("[cost] could not resolve provider for model=%s; cost may not resolve", model_name)

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
        "provider": provider,
        "model_name": model_name,
        "billing_unit_breakdown": breakdown,
        "conversation_id": attribution.get("conversation_id"),
        "sub_agent_id": attribution.get("sub_agent_id"),
        "scheduled_job_id": attribution.get("scheduled_job_id"),
        "sub_agent_config_version_id": attribution.get("sub_agent_config_version_id"),
        "catalog_id": attribution.get("catalog_id"),
    }


_RETRYABLE_STATUS = frozenset({408, 429})  # request-timeout / too-many-requests: transient


def _is_retryable(exc: Exception) -> bool:
    """Whether a flush failure is worth retrying. 5xx, plus the transient 4xx codes
    (408 Request Timeout, 429 Too Many Requests), and timeouts/connection/transport errors
    are transient and worth a retry. A non-transient 4xx (bad/expired token, malformed or
    rejected payload) will never succeed, so retrying it forever would wedge the single
    worker behind a poison batch. Unknown exception shapes are treated as non-retryable for
    the same reason."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code in _RETRYABLE_STATUS
    if isinstance(exc, httpx.RequestError):  # timeout / connection / transport
        return True
    return False


# Fields safe to emit in the dead-letter log. user_sub (an OIDC subject) and conversation_id
# correlate to a person, so they are PII and are redacted. Allow-list (not deny-list) so a new
# attribution field added to _build_record never leaks by default — it must be opted in here.
# Consequence: a dead-lettered record is NOT billing-recoverable from the log (its billing
# target, user_sub, is gone). The line is an operational LOSS ALERT — which provider/model/agent
# dropped usage and how much — not a replay source.
# TODO(durability): put a managed queue (e.g. SQS) in front of console-backend so a backend
#   outage buffers usage durably and attributably instead of dead-lettering to a lossy log. Keep
#   the gateway a dumb HTTP producer (no cloud SDK) — the queue lives in deployment infra, so the
#   callback stays cloud-agnostic. Prereq: gateway-batch-log ingestion must be idempotent on a
#   per-record key, or queue redelivery (and any replay) double-bills.
_DEAD_LETTER_SAFE_KEYS = (
    "provider",
    "model_name",
    "billing_unit_breakdown",
    "sub_agent_id",
    "scheduled_job_id",
    "sub_agent_config_version_id",
    "catalog_id",
)


class NannosCostLogger(CustomLogger):
    """Captures usage off the LLM hot path: each success event builds its record and
    enqueues it (non-blocking); a background worker batches enqueued records and POSTs them
    over the shared client. This keeps the per-call critical path free of a TCP/TLS
    handshake and a synchronous round-trip to console-backend.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict] | None = None
        self._worker: asyncio.Task | None = None

    def _ensure_worker(self) -> None:
        """Lazily create the queue + worker on the running proxy loop (first event)."""
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=_MAX_BUFFER)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            record = _build_record(kwargs, response_obj)
            if record is None:
                return
            if not CONSOLE_BACKEND_URL:
                logger.info(
                    "[cost] (no CONSOLE_BACKEND_URL) dropping usage record: provider=%s model=%s units=%s",
                    record.get("provider"),
                    record.get("model_name"),
                    sorted((record.get("billing_unit_breakdown") or {}).keys()),
                )
                return
            self._ensure_worker()
            try:
                self._queue.put_nowait(record)  # type: ignore[union-attr]
            except asyncio.QueueFull:
                # console-backend unreachable long enough to fill the buffer; drop rather
                # than grow memory, but dead-letter the payload so it stays recoverable. We
                # never break the LLM call on a logging failure.
                logger.error("[cost] ingest buffer full (%d); dead-lettering usage record", _MAX_BUFFER)
                self._dead_letter([record], None)
        except Exception as e:  # never break the LLM call on a logging failure
            logger.error("[cost] failed to enqueue usage: %s", e, exc_info=True)

    async def _run(self) -> None:
        """Drain the queue and POST records in batches until cancelled."""
        assert self._queue is not None
        while True:
            batch = [await self._queue.get()]
            # Opportunistically coalesce whatever else is already queued.
            while len(batch) < _FLUSH_MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._flush(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _flush(self, batch: list[dict]) -> None:
        headers = {"Content-Type": "application/json"}
        if GATEWAY_INGEST_TOKEN:
            headers["Authorization"] = f"Bearer {GATEWAY_INGEST_TOKEN}"
        last_exc: Exception | None = None
        for attempt in range(_FLUSH_MAX_RETRIES + 1):
            try:
                resp = await _get_client().post(
                    f"{CONSOLE_BACKEND_URL}{_INGEST_PATH}",
                    json={"logs": batch},
                    headers=headers,
                )
                resp.raise_for_status()
                return
            except Exception as e:  # a flush failure must not kill the worker
                last_exc = e
                if not _is_retryable(e) or attempt == _FLUSH_MAX_RETRIES:
                    break
                backoff = _FLUSH_BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "[cost] flush attempt %d/%d for %d record(s) failed (%s); retrying in %.1fs",
                    attempt + 1, _FLUSH_MAX_RETRIES + 1, len(batch), e, backoff,
                )
                await asyncio.sleep(backoff)
        # Retries exhausted (or a non-retryable error): dead-letter so the records survive
        # in log aggregation for later reconciliation, instead of vanishing silently.
        self._dead_letter(batch, last_exc)

    def _dead_letter(self, batch: list[dict], exc: Exception | None) -> None:
        """Last-resort handling for records we cannot deliver. Emit one redacted, non-PII line
        per record (marker `[cost][dead-letter]`, keys per _DEAD_LETTER_SAFE_KEYS) so an operator
        can see which provider/model/agent dropped usage and how much. user_sub/conversation_id
        are stripped, so these records are NOT billing-recoverable from the log — this is a loss
        alert, not a replay source. Durable, attributable recovery is the managed-queue TODO."""
        logger.error(
            "[cost] dead-lettering %d undeliverable usage record(s) — BILLING DATA LOST: %s",
            len(batch),
            exc,
        )
        for record in batch:
            safe = {k: record[k] for k in _DEAD_LETTER_SAFE_KEYS if k in record}
            logger.error("[cost][dead-letter] %s", json.dumps(safe, default=str))


proxy_handler_instance = NannosCostLogger()
