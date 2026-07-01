"""Tests for the StreamCoordinator seam (single-replica in-memory impl)."""

import pytest

from ringier_a2a_sdk.server.executor import (
    ActiveStreamInfo,
    InMemoryStreamCoordinator,
    get_stream_coordinator,
    set_stream_coordinator,
)


@pytest.mark.asyncio
async def test_try_register_claims_then_returns_active_for_steering():
    coord = InMemoryStreamCoordinator()
    ctx = "c-coord-claim"
    try:
        first = ActiveStreamInfo(context_id=ctx, task_id="t1", owner_sub="u1")
        # First caller claims the turn.
        assert await coord.try_register(first) is None
        # A concurrent send for the same conversation finds the active stream to steer into.
        second = ActiveStreamInfo(context_id=ctx, task_id="t2", owner_sub="u1")
        active = await coord.try_register(second)
        assert active is first
        # After release, the conversation can start a fresh turn again.
        await coord.release(ctx)
        assert await coord.try_register(second) is None
    finally:
        await coord.release(ctx)


@pytest.mark.asyncio
async def test_set_stream_coordinator_swaps_the_active_impl():
    original = get_stream_coordinator()
    try:
        replacement = InMemoryStreamCoordinator()
        set_stream_coordinator(replacement)
        assert get_stream_coordinator() is replacement
    finally:
        set_stream_coordinator(original)
