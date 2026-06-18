# Usage Event capture at the Gateway, pricing stays in the Rate Card

**Status:** accepted

We capture each LLM call's Usage Event with a **proxy-side LiteLLM `CustomLogger`** (not the in-app LangChain `CostTrackingCallback`), forwarding Nannos Cost Attribution to the proxy as `spend_logs_metadata`. Pricing remains in console-backend's **Rate Card** (`RateCardService.calculate_cost`); LiteLLM's computed `response_cost` is informational/cross-check only.

## Why

The proxy sees the **native** provider response — real provider+model identity, full streamed usage, and the `cache_creation`/`cache_read`/`reasoning` token breakdown — *before* OpenAI-format normalization discards detail. The in-app callback (4-A) only sees the normalized OpenAI shape, which collapses provider to `openai`, can drop Anthropic `cache_creation` tokens, and needs `stream_usage` forced. Capturing at the Gateway fixes all three at the root.

The Rate Card is **not** replaceable by LiteLLM cost data: it supports agent-specific pricing (per `sub_agent_config_version_id`), time-versioned rates (`effective_from`), and billing-unit fallback — none expressible in LiteLLM's flat per-model cost map. So pricing stays; only *capture* moves.

## Considered Options

- **4-A — keep the in-app LangChain callback.** Smaller change, attribution already wired via LangGraph tags, but inherits the fidelity defects above.
- **Retire the Rate Card, use LiteLLM spend tracking as source of truth.** Rejected — and not because of attribution (LiteLLM `spend_logs_metadata` carries arbitrary Nannos fields fine). It fails on the *pricing model*: per-sub-agent pricing is a shipped feature (`PricingConfigurationSection.tsx`, admin `RateCardsPage.tsx`; `sub_agent.pricing_config`) and LiteLLM's cost is per-model, not per-caller — faking it needs a `(model × sub-agent-version)` alias explosion incompatible with dynamic sub-agents. Plus time-versioned rates (`effective_from`) have no LiteLLM equivalent, and `analytics_service.get_cost_over_time` consumes the marked-up `total_cost_usd`. Ditching the Rate Card would require a product decision to drop per-sub-agent pricing + rate versioning.

## Consequences

- Attribution must reach the proxy per-request. Mechanism: ContextVars set at the request boundary + a custom httpx event hook stamping `x-litellm-spend-logs-metadata` on every outbound call (extends the existing `current_sub_agent_id` / `SubAgentIdMiddleware` pattern). Clients are cached per `(model_type, thinking_level)`, so attribution cannot be baked in at construction.
- New ingestion path: proxy `CustomLogger` → console-backend cost-ingestion → `RateCardService`.
- Spike risks: ContextVar propagation across async→threadpool (sync LangGraph tool nodes); whether `cache_creation`/`cache_read` persist as separate spend-log fields; LiteLLM `model_info` custom-pricing application bugs (issue #11975).

## Spike outcome (2026-06-18) — CONFIRMED, cache-creation worry refuted

Verified on `litellm:main-stable` / Bedrock `claude-sonnet-4.6`:
- The proxy CustomLogger retains the **native** breakdown — `cache_creation_input_tokens` (create=6814 / read=6814), reasoning tokens, and the real provider (`bedrock`) + model id. The earlier concern that OpenAI-format normalization drops Anthropic `cache_creation` is **refuted** on this version — it even surfaces in the client response as `prompt_tokens_details.cache_creation_tokens`.
- Attribution: all five Nannos fields ride `x-litellm-spend-logs-metadata` (set by a ContextVar→httpx hook on a shared, cached client), land in the logger's `metadata.spend_logs_metadata`, and persist into `LiteLLM_SpendLogs`. **50-way concurrency on one shared client showed zero attribution bleed.**
- Threadpool rule: `asyncio.to_thread` copies the context (the existing sandbox `to_thread` path is safe); only a raw `run_in_executor` loses contextvars (`copy_context().run` restores them).
- Operational note: the DB SpendLogs writer is batched/lossy under burst — another reason to treat the real-time CustomLogger as the capture path, not the DB table. See `spikes/litellm-proxy-verification/SPIKE-FINDINGS.md`.
