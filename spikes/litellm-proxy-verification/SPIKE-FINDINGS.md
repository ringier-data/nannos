# Spike findings — LiteLLM Gateway verification

Fill this in after running. Feed verdicts back into the ADRs and the spike checklist in `../../CONTEXT.md`.

- **LiteLLM version tested:** `main-stable` resolving to **v1.89.2** (per the proxy UI, 2026-06-18). Pin this tag/digest in `docker-compose.yml`.
- **Bedrock:** `nannos-dev-developer`, region `eu-central-1`, model `global.anthropic.claude-sonnet-4-6`
- **Date / runner:** 2026-06-18, local Docker (Darwin). Final run: **11 passed, 1 xfailed**.

## Results matrix

| Check | ADR | Verdict | Notes |
|---|---|---|---|
| 1 — thinking passthrough | 0003 | ✅ GO | `reasoning_effort` low/med/high all return reasoning_content + thinking_blocks on Bedrock 4.6; no 400s; `minimal→low` ok |
| 2a — first-token timeout | 0004 | ✅ GO | aborts ~5.1s (raw); caller SDK needs `max_retries=0` or 5s reads as ~17s |
| 2b — inter-chunk timeout | 0004 | ✅ GO | `stream_timeout` catches mid-stream stall **on the OpenAI path** |
| 2c — Bedrock stream timeout fires | 0004 | 🔴 **GATE TRIPPED** (xfail) | `stream_timeout=0.5s` **silently ignored** on Bedrock streaming (first chunk 1.5s, completed 3.1s, no abort) = #23375 |
| 3 — cost fidelity (cache/provider) | 0002 | ✅ GO | create=6814 / read=6814 cache tokens; provider=`bedrock`, model=`global.anthropic.claude-sonnet-4-6`; reasoning tokens captured |
| 3 — SpendLogs persist | 0002 | ✅ GO | persists to `LiteLLM_SpendLogs.metadata->spend_logs_metadata`; batch-flushed (lagged); burst lossy → real-time CustomLogger preferred |
| 4a — attribution round-trip | 0002 | ✅ GO | all 5 fields round-trip via `x-litellm-spend-logs-metadata`; CustomLogger sees `metadata.spend_logs_metadata` |
| 4b — concurrency isolation | 0002 | ✅ GO | 50 concurrent on one shared client, zero bleed |
| 4c — to_thread boundary rule | 0002 | ✅ documented | `asyncio.to_thread` copies context (sandbox path SAFE); raw `run_in_executor` lossy; `copy_context().run` preserves |

### Key findings
- **🔴 ADR-0004 gate is TRIPPED.** The same `stream_timeout` that fires cleanly on the OpenAI/mock path (2a, 2b) is **silently ignored on Bedrock streaming** (2c) — exactly #23375. **A client-side inter-chunk watchdog (3-C) is mandatory before go-live.** The 2c test is `xfail(strict=True)`, so it doubles as a tripwire: if a future LiteLLM fixes this, it XPASSes and prompts re-evaluating 3-C.
- **✅ ADR-0002 cache-creation worry REFUTED.** `cache_creation_input_tokens` (and cache_read, reasoning) is fully retained by the proxy CustomLogger — and even survives into the client OpenAI-format response (`prompt_tokens_details.cache_creation_tokens`). The migration does **not** lose Anthropic cache-creation fidelity on this version.
- **✅ ADR-0002 attribution + isolation proven.** 5 Nannos fields ride the ContextVar→header hook on a shared client, persist into SpendLogs, and survive 50-way concurrency with zero bleed.
- **✅ ADR-0003 thinking works; "medium ≠ 10k" confirmed empirically** — `medium` resolved to ~25 reasoning tokens on a trivial prompt (provider-determined, not the old fixed 10k budget).
- **Caller retries matter:** `num_retries` (router) + per-deployment `max_retries` (provider client) + the *caller's* SDK `max_retries` all stack. Production wiring must set caller retries deliberately (the 17s→5s lesson).
- **SpendLogs DB is batched/lossy under burst** (50 calls → ~5 immediate rows) — reinforces ADR-0002's choice of the real-time CustomLogger as the capture path, not the DB.

## Decisions resolved by this run

1. **ADR-0004 gate → RESOLVED: watchdog REQUIRED.** The proxy does **not** enforce timeouts on Bedrock streaming (2c, #23375), though it does on the OpenAI path (2a/2b). ⇒ Implement a client-side inter-chunk watchdog (3-C) before go-live; treat proxy `stream_timeout` as a best-effort outer bound only.
2. **ADR-0003 → CONFIRMED.** `reasoning_effort` is the right mechanism; no 400s on Claude 4.6; budgets are provider-determined (medium ≈ 25 tok here, not 10k). Keep only the small `thinking_level→reasoning_effort` map in-app.
3. **ADR-0002 → CONFIRMED + worry refuted.** CustomLogger retains cache_creation/cache_read/reasoning + real provider/model; attribution travels via `metadata.spend_logs_metadata` (header `x-litellm-spend-logs-metadata`) and persists. Use the real-time CustomLogger (not the batched DB) as the production capture path.

## Follow-ups for the migration (not this spike)
- Build the **3-C client-side inter-chunk watchdog** wrapping `astream` (`asyncio.wait_for` per chunk).
- Set caller-side `max_retries`/`num_retries` deliberately (avoid the 17s stacking).
- Pin the exact LiteLLM digest; re-run 2c on upgrades (the xfail tripwire).

## Raw artifacts
- Captured events: `captured/events.jsonl`
- Proxy logs: `docker compose logs litellm`
