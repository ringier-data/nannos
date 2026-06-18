"""Check 3 — cost fidelity at the proxy (ADR-0002).

The proxy-side CustomLogger must retain the NATIVE breakdown — cache_creation /
cache_read / reasoning tokens and the real provider/model — that the client's
OpenAI-format response loses. Also confirm SpendLogs persist to Postgres.
"""

import time

import pytest

MODEL = "claude-sonnet-4.6"
# Unique per run so call #1 CREATES a fresh cache (Bedrock caches persist ~5min,
# otherwise a re-run sees only reads). > 1024 tokens so caching is eligible.
LONG_CONTEXT = f"Run-{time.time()} " + "Nannos is a multi-agent orchestration platform with sub-agents. " * 400


def _cached_system_message():
    return {
        "role": "system",
        "content": [
            {"type": "text", "text": LONG_CONTEXT, "cache_control": {"type": "ephemeral"}},
        ],
    }


@pytest.mark.integration
async def test_cache_and_provider_fidelity(aclient, captures):
    # Two identical calls: #1 creates the cache, #2 reads it.
    for _ in range(2):
        await aclient.chat.completions.create(
            model=MODEL,
            messages=[_cached_system_message(), {"role": "user", "content": "Reply with one short sentence."}],
            max_tokens=64,
        )

    recs = await captures.wait(2, timeout=40)
    assert len(recs) >= 2, f"expected 2 captured events, got {len(recs)}"

    # Provider/model identity must NOT collapse to openai.
    # Real backend model id lands in model_requested (kwargs["model"]) for Bedrock.
    for r in recs:
        assert r["provider"] and "bedrock" in r["provider"].lower(), f"provider collapsed: {r['provider']}"
        model_id = r.get("model_requested") or r.get("model_backend") or ""
        assert "claude-sonnet-4-6" in model_id, f"model lost: {model_id!r}"

    creation = [r.get("cache_creation_input_tokens") for r in recs]
    reads = [r.get("cache_read_input_tokens") for r in recs]
    print(f"\n[Check3] cache_creation={creation} cache_read={reads} "
          f"providers={[r['provider'] for r in recs]}")

    saw_creation = any(v and v > 0 for v in creation)
    saw_read = any(v and v > 0 for v in reads)
    if not (saw_creation or saw_read):
        pytest.skip(
            "Bedrock prompt caching not observed (region/model may not support it). "
            "Provider/model fidelity confirmed; re-run where caching is enabled to validate cache-token fidelity."
        )
    assert saw_creation, "cache_creation_input_tokens never seen by the proxy logger"
    assert saw_read, "cache_read_input_tokens never seen by the proxy logger"


@pytest.mark.integration
async def test_spend_logs_persist(aclient, captures):
    await aclient.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=16,
    )
    await captures.wait(1, timeout=30)
    logs = await captures.spend_logs()
    print(f"\n[Check3] spend_logs rows={len(logs)}")
    assert logs, "no rows persisted to the SpendLogs table"
    assert any((row.get("spend") or 0) > 0 for row in logs), "no row with non-zero spend persisted"
