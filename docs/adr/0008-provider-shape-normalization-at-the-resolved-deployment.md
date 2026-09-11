---
status: accepted (2026-09-11)
---

# Provider-shape normalization belongs at the resolved deployment, not at the requested alias

Requests are shaped once, app-side, in Anthropic's idiom — one client for every provider
(ADR-0001). Wherever a provider needs a different shape, the rewriting happens **proxy-side in
the pre-call deployment hook**, keyed on the deployment that will actually serve the call, and
never app-side on the alias that was requested. With failover in place, the requested alias no
longer names the serving provider: `claude-sonnet-4.6` can be answered by Azure OpenAI. Any
rewrite keyed on the alias is therefore keyed on a guess that is wrong precisely when it matters.

Consequently the prompt-cache rule is an **allowlist**: `cache_control` markers are kept for
deployments that speak Anthropic's format and stripped for every other. It used to be a denylist
naming `vertex_ai`/`gemini`, which fails open — an unlisted provider received Anthropic markers
and could reject the request outright.

## Why

- **A failover that itself fails is worse than no failover.** The denylist made the fallback
  attempt carry markers the fallback provider may not accept, so the safety net would tear at the
  exact moment it was deployed. An allowlist fails the other way: an unlisted provider loses
  prompt caching and costs more. Losing a discount is recoverable and measurable; a request that
  400s during a provider outage is neither.
- **Only the proxy knows who served.** The router picks a deployment per attempt, after the app
  has built its request. `get_model_provider(alias)` reads `litellm_provider` off one alias's
  `model_info` and cannot represent "Bedrock, unless it's down, then Azure". The pre-call
  deployment hook runs once per attempt with the resolved deployment in hand, which is the only
  place the question has a correct answer.
- **It keeps the app's one-client property intact.** The alternative — teaching the app to shape
  per resolved provider — means reading back what served and re-shaping on the next turn, putting
  outage-only complexity in the hottest path in the system and reintroducing the provider
  branching ADR-0001 removed. We rejected it: the cost is permanent, the benefit is occasional.
- **The hook was already written for this.** `_strip_cache_control_entries` is copy-on-write
  specifically so "mutating shared message dicts would leak the strip into a fallback attempt on
  a provider that wants its markers." The mechanism anticipated failover; only the rule's polarity
  had to change.

## Consequences

- A provider that honours Anthropic-style `cache_control` but is missing from the allowlist
  silently loses prompt caching, which shows up as a cost regression rather than an error. The
  allowlist has to be maintained as providers are added; the boot-time gateway settings check is
  where that omission is meant to become visible.
- `thinking_blocks` replay remains app-side and alias-keyed
  (`_thinking_round_trip_provider`). It is not covered by this ADR's mechanism. Extended thinking
  is dropped on a fallback turn rather than replayed, which is accepted: an answer without
  extended thinking beats no answer, and the primary's own turns are unaffected.
