"""Check 2 — timeout separation on streaming (ADR-0004). THE GATE.

If the proxy does not enforce BOTH first-token and inter-chunk timeouts on
Bedrock streaming, ADR-0004 escalation to a client-side watchdog (3-C) is
mandatory before go-live. Failures here are the gate tripping, not bugs in the
spike (the user chose "start simple" expecting this might fail).

Mock delays (docker-compose env): FIRST_TOKEN_DELAY_S=30, INTER_CHUNK_STALL_S=30.
Per-model stream_timeout in config.yaml: mock=5s, bedrock-tight=0.5s.
"""

import asyncio
import time

import pytest


async def _consume_stream(aclient, model, *, guard: float):
    """Consume a streamed completion. Returns (proxy_aborted, elapsed, first_chunk_at)."""
    start = time.monotonic()
    first_chunk_at = None

    async def _run():
        nonlocal first_chunk_at
        stream = await aclient.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "stream a sentence please"}],
            max_tokens=256,
            stream=True,
        )
        async for chunk in stream:
            if first_chunk_at is None and chunk.choices and chunk.choices[0].delta:
                first_chunk_at = time.monotonic() - start

    try:
        await asyncio.wait_for(_run(), timeout=guard)
        return False, time.monotonic() - start, first_chunk_at  # completed, proxy did NOT abort
    except asyncio.TimeoutError:
        return False, time.monotonic() - start, first_chunk_at  # guard hit => proxy did NOT abort in time
    except Exception as e:  # proxy raised a timeout/error => abort enforced
        print(f"\n[Check2] {model} aborted with {type(e).__name__}: {str(e)[:160]}")
        return True, time.monotonic() - start, first_chunk_at


async def test_2a_first_token_timeout(aclient):
    """Slow first token (30s) must be aborted by stream_timeout (5s)."""
    aborted, elapsed, _ = await _consume_stream(aclient, "mock-first-token-delay", guard=20)
    print(f"\n[Check2a] aborted={aborted} elapsed={elapsed:.1f}s")
    assert aborted, "first-token timeout NOT enforced (mock delayed 30s, stream_timeout=5s)"
    assert elapsed < 15, f"aborted too late ({elapsed:.1f}s); expected ~5s"


async def test_2b_inter_chunk_timeout(aclient):
    """First chunk fast, then 30s stall: does stream_timeout (5s) catch the inter-chunk gap?

    If this FAILS, stream_timeout is first-token-only -> client-side inter-chunk
    watchdog (3-C) required (ADR-0004 gate).
    """
    aborted, elapsed, first_at = await _consume_stream(aclient, "mock-inter-chunk-stall", guard=20)
    print(f"\n[Check2b] aborted={aborted} elapsed={elapsed:.1f}s first_chunk_at={first_at}")
    assert first_at is not None and first_at < 5, "first chunk should arrive promptly"
    assert aborted and elapsed < 15, (
        "INTER-CHUNK stall NOT aborted by proxy -> GATE TRIPPED: client-side watchdog (3-C) required"
    )


@pytest.mark.integration
@pytest.mark.xfail(
    strict=True,
    reason="#23375: Bedrock streaming silently ignores stream_timeout -> ADR-0004 gate TRIPPED, "
    "client-side watchdog (3-C) required. XPASS here means a newer LiteLLM fixed it -> revisit 3-C.",
)
async def test_2c_bedrock_stream_timeout_fires(aclient):
    """Real Bedrock streaming with stream_timeout=0.5s must actually abort.

    If it completes normally, reproduces #23375 (timeout silently ignored on
    Bedrock streaming) -> GATE TRIPPED.
    """
    aborted, elapsed, first_at = await _consume_stream(aclient, "claude-sonnet-4.6-tighttimeout", guard=30)
    print(f"\n[Check2c] aborted={aborted} elapsed={elapsed:.1f}s first_chunk_at={first_at}")
    assert aborted, (
        "Bedrock streaming did NOT honor stream_timeout=0.5s -> reproduces #23375 -> "
        "GATE TRIPPED: client-side watchdog (3-C) required"
    )
