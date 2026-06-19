# Timeout strategy: proxy-enforced first, watchdog as gated contingency

**Status:** accepted

We rely on the LiteLLM Proxy's `stream_timeout` (first-token) and `timeout`/`request_timeout` (total) on a **pinned** LiteLLM version, rather than building a client-side inter-chunk watchdog up front. This is the deliberately simple starting point.

## Why

The original incident (Bedrock 5-min hang) was rooted in synchronous boto3, which made first-token vs inter-chunk timeout separation impossible. The migration makes the path async, which already removes the root cause and enables proxy-side timeouts. Starting with proxy-only timeouts keeps the app simple.

## The risk we are accepting (read before assuming this is done)

LiteLLM does **not** cleanly expose a separate first-token *and* inter-chunk timeout pair — `stream_timeout` is one streaming timeout, documented inconsistently. And there are open bugs against our exact case: `timeout` silently ignored for **Bedrock streaming** ([#23375](https://github.com/BerriAI/litellm/issues/23375)) and `stream_timeout` not enforced on the first chunk ([#19909](https://github.com/BerriAI/litellm/issues/19909)). So proxy-only timeouts could re-create the original failure mode.

## Gated contingency (mandatory)

The migration spike **must verify** that first-token and inter-chunk timeouts actually fire on Bedrock streaming on the pinned LiteLLM version. If they do not, escalate before go-live to **3-C**: a client-side inter-chunk watchdog wrapping the async stream (`asyncio.wait_for` per chunk, cancel on stall), with proxy timeouts as outer bounds. Do not ship proxy-only if the spike shows the bugs still bite.

## Spike outcome (2026-06-18) — GATE TRIPPED → 3-C required

Verified on `litellm:main-stable` (v1.89.2) against Bedrock `claude-sonnet-4.6` (eu-central-1). The proxy `stream_timeout` fires cleanly on the OpenAI/mock path (first-token ~5s, inter-chunk caught) but is **silently ignored on Bedrock streaming** (`stream_timeout=0.5s`: first chunk at 1.5s, stream completed at 3.1s, no abort) — i.e. #23375 reproduces. **Decision: proxy-only (3-A) is insufficient; the client-side inter-chunk watchdog (3-C) is required before go-live.** Proxy `stream_timeout`/`timeout` remain as best-effort outer bounds. Regression tripwire: `spikes/litellm-proxy-verification/tests/test_timeouts.py::test_2c_*` is `xfail(strict=True)` — an XPASS on a LiteLLM upgrade means Bedrock timeouts now work and 3-C can be reconsidered. Also: caller-side SDK `max_retries` stacks on top of proxy timeouts (a clean 5s read as ~17s) — set it deliberately. See `spikes/litellm-proxy-verification/SPIKE-FINDINGS.md`.

## Non-streaming calls (timeout + retry)

The watchdog (3-C) only wraps the streaming graph path. Non-streaming gateway calls (`.ainvoke()`: tool-risk scoring, file analysis, scheduler condition/message generation) are bounded **natively at the proxy**, not per-call-site: `request_timeout: 600` caps total duration and `num_retries: 2` retries transient failures/timeouts. This replaces the per-call boto3 `read_timeout`/`retries` config dropped in the migration; keep the per-model `max_retries: 0` so the underlying SDK doesn't stack a second retry layer on top of `num_retries`.
