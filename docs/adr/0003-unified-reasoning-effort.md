# Extended thinking via unified `reasoning_effort`

**Status:** accepted

Extended thinking is expressed through LiteLLM's unified `reasoning_effort` parameter, not explicit `thinking.budget_tokens`. The app maps its `thinking_level` (minimal/low/medium/high) to a `reasoning_effort` value; LiteLLM owns the per-model wire translation (`budget_tokens` vs `output_config.effort` vs `thinking:{type:"adaptive"}`) and the Bedrock 1024-token floor. The previous `get_thinking_budget()` token-budget map is deleted.

## Why

The explicit `budget_tokens` interface is being deprecated on the models we already run: Claude 4.6 maps effort to `output_config.effort`, and Opus 4.8 rejects `thinking.type=enabled` (needs adaptive + effort). Keeping explicit budgets would mean per-model special-casing that fights the provider's direction. `reasoning_effort` is forward-compatible and keeps the gnarly translation out of our code.

## Consequences

- **Behavior shift to validate:** `medium` no longer means exactly 10,000 thinking tokens — it resolves to Anthropic's medium-effort budget. Latency/quality/cost may move; verify in the spike. This is the likely "why did medium stop being 10k?" question for a future reader.
- `minimal` has no exact effort equivalent; maps to `low` (Bedrock floors it at 1024 anyway).
- The `thinking_level` vocabulary and which level a task uses remain product policy in the app.

## Spike outcome (2026-06-18) — CONFIRMED

On `litellm:main-stable` / Bedrock `claude-sonnet-4.6`: `reasoning_effort` ∈ {low, medium, high} all return `reasoning_content` + `thinking_blocks` with no 400s, and `minimal→low` is accepted. Budgets are provider-determined — `medium` resolved to ~25 reasoning tokens on a trivial prompt, empirically confirming "medium ≠ 10k". Keep the small `thinking_level→reasoning_effort` map in-app; the fixed token-budget map stays deleted. See `spikes/litellm-proxy-verification/SPIKE-FINDINGS.md`.
