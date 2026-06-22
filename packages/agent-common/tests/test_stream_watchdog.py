"""Tests for the client-side streaming watchdog."""

import asyncio

import pytest

from agent_common.core.stream_watchdog import StreamStallError, watch_stream


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


async def test_inter_chunk_timeout():
    collected = []
    with pytest.raises(StreamStallError) as ei:
        async for c in watch_stream(_gen(["a", "b"], gap=5), first_timeout=1, chunk_timeout=0.2):
            collected.append(c)
    assert collected == ["a"]  # first chunk arrived, then the stall was caught
    assert "inter-chunk" in str(ei.value)
