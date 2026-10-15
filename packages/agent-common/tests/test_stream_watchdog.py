"""Tests for the client-side streaming watchdog."""

import asyncio

import pytest

from agent_common.core.stream_watchdog import (
    StreamStallError,
    first_token_timeout,
    inter_chunk_timeout,
    stall_resume_budget_multiplier,
    watch_stream,
    watch_stream_with_resume,
)


async def _gen(items, *, first_delay=0.0, gap=0.0):
    if first_delay:
        await asyncio.sleep(first_delay)
    for i, item in enumerate(items):
        if i > 0 and gap:
            await asyncio.sleep(gap)
        yield item


async def test_passes_through_all_chunks():
    out = [c async for c in watch_stream(_gen(["a", "b", "c"]), first_timeout=1, chunk_timeout=1)]
    assert out == ["a", "b", "c"]


async def test_first_token_timeout():
    with pytest.raises(StreamStallError) as ei:
        async for _ in watch_stream(_gen(["a"], first_delay=5), first_timeout=0.2, chunk_timeout=5):
            pass
    assert "first-token" in str(ei.value)
    assert ei.value.phase == "first-token"
    assert ei.value.budget == 0.2


async def test_inter_chunk_timeout():
    collected = []
    with pytest.raises(StreamStallError) as ei:
        async for c in watch_stream(_gen(["a", "b"], gap=5), first_timeout=1, chunk_timeout=0.2):
            collected.append(c)
    assert collected == ["a"]  # first chunk arrived, then the stall was caught
    assert "inter-chunk" in str(ei.value)
    assert ei.value.phase == "inter-chunk"
    assert ei.value.budget == 0.2


def test_first_token_budget_default_accommodates_cold_cache_ttft(monkeypatch):
    # A ~150k-token prompt on a cold Bedrock cache takes 30s+ of ingestion before
    # the first token; the default must not classify that as a hang. Assert a floor
    # rather than the exact constant so retuning the default doesn't require a
    # byte-identical edit here.
    monkeypatch.delenv("LLM_FIRST_TOKEN_TIMEOUT", raising=False)
    assert first_token_timeout() >= 60.0


def test_budgets_respect_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT", "12.5")
    monkeypatch.setenv("LLM_INTER_CHUNK_TIMEOUT", "45")
    assert first_token_timeout() == 12.5
    assert inter_chunk_timeout() == 45.0


def test_budgets_survive_misconfiguration(monkeypatch):
    # Unparsable values fall back to the default; 0/negative (which would make
    # asyncio.wait_for trip instantly on every request) are clamped, and the
    # multiplier can never make the resume less generous than the failed pass.
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT", "90s")
    assert first_token_timeout() >= 60.0
    monkeypatch.setenv("LLM_INTER_CHUNK_TIMEOUT", "0")
    assert inter_chunk_timeout() >= 1.0
    monkeypatch.setenv("LLM_STALL_RESUME_BUDGET_MULTIPLIER", "0")
    assert stall_resume_budget_multiplier() >= 1.0


async def test_resume_recovers_from_first_token_stall():
    calls = []

    def make_stream(resuming):
        calls.append(resuming)
        if resuming:
            return _gen(["a", "b"])
        return _gen(["never"], first_delay=5)

    out = [
        c
        async for c in watch_stream_with_resume(
            make_stream, first_timeout=0.1, chunk_timeout=1, recovery_part="RECOVERING"
        )
    ]
    assert calls == [False, True]
    assert out == ["RECOVERING", "a", "b"]


async def test_resume_scales_only_the_tripped_budget():
    # First-token stall at 0.1s → resume budget is 0.1 × multiplier (>=3 default),
    # so a resume whose first part takes 0.2s must now fit the scaled budget.
    def make_stream(resuming):
        return _gen(["a"], first_delay=0.2 if resuming else 5)

    out = [c async for c in watch_stream_with_resume(make_stream, first_timeout=0.1, chunk_timeout=1)]
    assert out == ["a"]


async def test_second_stall_propagates():
    def make_stream(resuming):
        return _gen(["never"], first_delay=5)

    with pytest.raises(StreamStallError):
        async for _ in watch_stream_with_resume(make_stream, first_timeout=0.1, chunk_timeout=1):
            pass


async def test_empty_resume_fails_loudly():
    # A resume that yields nothing means the checkpoint had no pending work (the
    # turn's input may never have been persisted) — re-raise the original stall
    # instead of letting the caller serve stale prior-turn state.
    async def _empty():
        return
        yield  # pragma: no cover — makes this an async generator

    def make_stream(resuming):
        if resuming:
            return _empty()
        return _gen(["never"], first_delay=5)

    with pytest.raises(StreamStallError) as ei:
        async for _ in watch_stream_with_resume(make_stream, first_timeout=0.1, chunk_timeout=1):
            pass
    assert ei.value.phase == "first-token"


# --- await_with_keepalive: silent work inside a graph node ---


def _recording_writer(monkeypatch):
    parts: list = []
    monkeypatch.setattr("agent_common.core.stream_watchdog._stream_writer", lambda: parts.append)
    # The smallest budget the env allows: one keepalive per second (half of it, floored at 1s).
    monkeypatch.setenv("LLM_INTER_CHUNK_TIMEOUT", "2")
    return parts


async def test_keepalive_flows_while_silent_work_runs(monkeypatch):
    from agent_common.core.stream_watchdog import KEEPALIVE_EVENT, await_with_keepalive

    parts = _recording_writer(monkeypatch)

    async def slow():
        await asyncio.sleep(2.3)
        return "scored"

    assert await await_with_keepalive(slow(), source="test") == "scored"
    assert [p[0] for p in parts] == [KEEPALIVE_EVENT, KEEPALIVE_EVENT]
    assert parts[0][1]["source"] == "test"


async def test_on_slow_fires_once_and_only_when_the_wait_is_noticeable(monkeypatch):
    from agent_common.core.stream_watchdog import await_with_keepalive

    _recording_writer(monkeypatch)
    calls: list[str] = []

    async def say():
        calls.append("slow")

    async def quick():
        return 1

    async def slow():
        await asyncio.sleep(0.3)
        return 2

    assert await await_with_keepalive(quick(), source="t", on_slow=say, slow_after=0.1) == 1
    assert calls == []
    assert await await_with_keepalive(slow(), source="t", on_slow=say, slow_after=0.1) == 2
    assert calls == ["slow"]


async def test_outside_a_graph_it_is_a_plain_await(monkeypatch):
    from agent_common.core.stream_watchdog import await_with_keepalive

    monkeypatch.setattr("agent_common.core.stream_watchdog._stream_writer", lambda: None)

    async def work():
        return "done"

    assert await await_with_keepalive(work(), source="t") == "done"


async def test_cancelling_the_wait_cancels_the_work(monkeypatch):
    from agent_common.core.stream_watchdog import await_with_keepalive

    _recording_writer(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    waiter = asyncio.create_task(await_with_keepalive(work(), source="t"))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cancelled.is_set()
