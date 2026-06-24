"""Tests for the sub-agent stream stall timeout helper.

Covers the behavior introduced to prevent the orchestrator's sub-agent consumer
loop from hanging silently when a sub-agent stops producing items (parked LLM
call, unresponsive provider, MCP content-type error, DB lock, etc.).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging

import pytest

from agent_common.a2a.stream_events import ErrorEvent
from app.middleware.dynamic_tool_dispatch import _iter_subagent_stream_with_stall_timeout


class _NeverYields:
    """Async iterator that never yields and can be closed."""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> "_NeverYields":
        return self

    async def __anext__(self):
        # Park forever; the wrap_for timeout in the helper must cancel us.
        await asyncio.Event().wait()
        raise AssertionError("should never reach here")  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


class _YieldsThenStalls:
    """Yields the supplied items, then parks forever."""

    def __init__(self, items: list) -> None:
        self._items = list(items)
        self.closed = False

    def __aiter__(self) -> "_YieldsThenStalls":
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("should never reach here")  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_stall_timeout_yields_error_event_and_logs_heartbeat(caplog):
    """A stream that never yields hits the hard cap, logs heartbeat + error,
    yields exactly one ErrorEvent, and does not leak an exception."""
    caplog.set_level(logging.DEBUG, logger="app.middleware.dynamic_tool_dispatch")
    upstream = _NeverYields()

    collected = []
    async for item in _iter_subagent_stream_with_stall_timeout(
        upstream,
        subagent_type="stuck-agent",
        orchestrator_conversation_id="conv-123",
        subagent_thread_id="sub-thread-9",
        tick_seconds=0.05,
        hard_cap_seconds=0.18,  # ≥ 3 ticks → at least one heartbeat before the cap
    ):
        collected.append(item)

    # Exactly one ErrorEvent yielded, with a clear stall message mentioning the agent.
    assert len(collected) == 1, collected
    error = collected[0]
    assert isinstance(error, ErrorEvent)
    assert "stuck-agent" in error.error
    assert "stall" in error.error.lower()

    # Heartbeat warning emitted at least once while we waited.
    heartbeats = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Still waiting on sub-agent" in r.getMessage()
    ]
    assert heartbeats, "expected at least one heartbeat warning before hard cap"
    assert any("stuck-agent" in r.getMessage() for r in heartbeats)

    # Hard-cap error log emitted exactly once with diagnostic context.
    stall_errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "Sub-agent stream stalled" in r.getMessage()
    ]
    assert len(stall_errors) == 1
    msg = stall_errors[0].getMessage()
    assert "stuck-agent" in msg
    assert "conv-123" in msg
    assert "sub-thread-9" in msg

    # Upstream iterator was closed (no parked __anext__ left behind).
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_stall_timeout_passes_items_through_then_stalls():
    """Items that arrive in time are forwarded unchanged; only a subsequent
    stall produces an ErrorEvent. The stall window resets after each item."""
    upstream = _YieldsThenStalls(["a", "b", "c"])

    collected = []
    async for item in _iter_subagent_stream_with_stall_timeout(
        upstream,
        subagent_type="partial-agent",
        orchestrator_conversation_id=None,
        subagent_thread_id="?",
        tick_seconds=0.05,
        hard_cap_seconds=0.12,
    ):
        collected.append(item)

    assert collected[:3] == ["a", "b", "c"]
    assert isinstance(collected[-1], ErrorEvent)
    assert "partial-agent" in collected[-1].error
    assert upstream.closed is True


class _CancelSensitiveIterator:
    """Async iterator whose in-flight ``__anext__`` MUST NOT be cancelled by a
    heartbeat tick. If the helper cancels the pending task between ticks, the
    iterator marks itself poisoned and stops yielding — modeling real network
    / model streams that cannot survive mid-frame cancellation.

    Each value takes ``per_item_seconds`` to arrive. With a tick smaller than
    that, the helper must observe at least one heartbeat WITHOUT cancelling
    the in-flight read.
    """

    def __init__(self, items: list, per_item_seconds: float) -> None:
        self._items = list(items)
        self._per_item_seconds = per_item_seconds
        self.poisoned = False
        self.cancel_count = 0

    def __aiter__(self) -> "_CancelSensitiveIterator":
        return self

    async def __anext__(self):
        if self.poisoned:
            raise StopAsyncIteration
        if not self._items:
            raise StopAsyncIteration
        try:
            await asyncio.sleep(self._per_item_seconds)
        except asyncio.CancelledError:
            self.cancel_count += 1
            self.poisoned = True
            raise
        return self._items.pop(0)


@pytest.mark.asyncio
async def test_heartbeat_tick_does_not_cancel_in_flight_read(caplog):
    """A long-latency item that takes longer than one tick to arrive must
    still be delivered — the heartbeat must not cancel the pending __anext__.
    Regression guard for cancellation-sensitive streams (real model/network
    streams that get poisoned by mid-frame cancellation)."""
    caplog.set_level(logging.WARNING, logger="app.middleware.dynamic_tool_dispatch")

    # Each item takes 0.12s to arrive; tick is 0.05s → at least one heartbeat
    # before each item lands. Hard cap is well above per-item latency so we
    # should never actually trip the stall path.
    upstream = _CancelSensitiveIterator(items=["x", "y"], per_item_seconds=0.12)

    collected = []
    async for item in _iter_subagent_stream_with_stall_timeout(
        upstream,
        subagent_type="slow-but-alive",
        orchestrator_conversation_id="conv-slow",
        subagent_thread_id="t-slow",
        tick_seconds=0.05,
        hard_cap_seconds=2.0,
    ):
        collected.append(item)

    # Both items delivered intact — heartbeat did not poison the stream.
    assert collected == ["x", "y"]
    assert upstream.cancel_count == 0
    assert upstream.poisoned is False

    # And we still observed heartbeat warnings — proving the tick fired but
    # left the pending read alone.
    heartbeats = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Still waiting on sub-agent" in r.getMessage()
    ]
    assert heartbeats, "expected heartbeat warning while waiting for slow item"


@pytest.mark.asyncio
async def test_on_heartbeat_invoked_per_tick_with_waited_seconds():
    """While the sub-agent is silent but under the hard cap, ``on_heartbeat`` is
    called once per tick with the cumulative seconds waited — this is what the
    caller uses to emit a keepalive that resets the orchestrator watchdog. The
    callback may be async."""
    upstream = _NeverYields()
    waited_values: list[float] = []

    async def on_heartbeat(waited: float) -> None:
        waited_values.append(waited)

    collected = []
    async for item in _iter_subagent_stream_with_stall_timeout(
        upstream,
        subagent_type="busy-agent",
        orchestrator_conversation_id="conv-hb",
        subagent_thread_id="t-hb",
        tick_seconds=0.05,
        hard_cap_seconds=0.18,  # ≥ 3 ticks → at least one heartbeat before the cap
        on_heartbeat=on_heartbeat,
    ):
        collected.append(item)

    # Heartbeat fired at least once before the hard cap, with a positive waited time.
    assert waited_values, "expected on_heartbeat to be called at least once"
    assert all(w > 0 for w in waited_values)
    # Still ends with the single ErrorEvent (heartbeats are a side channel, not items).
    assert len(collected) == 1 and isinstance(collected[0], ErrorEvent)
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_on_heartbeat_failure_is_swallowed():
    """A throwing heartbeat callback must not break the stall loop — keepalive
    emission is best-effort."""
    upstream = _NeverYields()

    def boom(_waited: float) -> None:
        raise RuntimeError("stream_writer exploded")

    collected = []
    async for item in _iter_subagent_stream_with_stall_timeout(
        upstream,
        subagent_type="busy-agent",
        orchestrator_conversation_id="conv-boom",
        subagent_thread_id="t-boom",
        tick_seconds=0.05,
        hard_cap_seconds=0.18,
        on_heartbeat=boom,
    ):
        collected.append(item)

    # Despite the callback raising every tick, the loop still completes with the
    # single ErrorEvent and closes the upstream.
    assert len(collected) == 1 and isinstance(collected[0], ErrorEvent)
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_stall_timeout_completes_cleanly_when_upstream_finishes():
    """A normal stream that ends via StopAsyncIteration produces no ErrorEvent."""

    async def upstream():
        for x in (1, 2, 3):
            yield x

    collected = []
    async for item in _iter_subagent_stream_with_stall_timeout(
        upstream(),
        subagent_type="happy-agent",
        orchestrator_conversation_id="conv-ok",
        subagent_thread_id="t-ok",
        tick_seconds=1.0,
        hard_cap_seconds=5.0,
    ):
        collected.append(item)

    assert collected == [1, 2, 3]


@pytest.mark.asyncio
async def test_dispatch_id_threaded_through_logs_and_error_event(caplog):
    """The per-dispatch id generated at wrapper construction must appear on
    the entry log, the heartbeat warning, the stall-error log, and in the
    metadata of the yielded ErrorEvent."""
    caplog.set_level(logging.INFO, logger="app.middleware.dynamic_tool_dispatch")
    upstream = _NeverYields()

    collected = []
    async for item in _iter_subagent_stream_with_stall_timeout(
        upstream,
        subagent_type="stuck-agent",
        orchestrator_conversation_id="conv-XYZ",
        subagent_thread_id="t-shared",
        tick_seconds=0.05,
        hard_cap_seconds=0.18,
    ):
        collected.append(item)

    assert len(collected) == 1
    error = collected[0]
    assert isinstance(error, ErrorEvent)
    dispatch_id = error.data.metadata.get("dispatch_id")
    assert isinstance(dispatch_id, str) and len(dispatch_id) == 8

    entry_logs = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "Starting sub-agent dispatch" in r.getMessage()
    ]
    assert any(dispatch_id in r.getMessage() for r in entry_logs), entry_logs

    heartbeats = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Still waiting on sub-agent" in r.getMessage()
    ]
    assert heartbeats and all(dispatch_id in r.getMessage() for r in heartbeats)

    stall_errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "Sub-agent stream stalled" in r.getMessage()
    ]
    assert len(stall_errors) == 1
    assert dispatch_id in stall_errors[0].getMessage()

    md = error.data.metadata
    assert md.get("subagent_type") == "stuck-agent"
    assert md.get("orchestrator_conversation_id") == "conv-XYZ"
    assert md.get("subagent_thread_id") == "t-shared"


@pytest.mark.asyncio
async def test_concurrent_wrappers_for_same_thread_id_get_distinct_dispatch_ids(caplog):
    """Two concurrent wrappers that share the same subagent_thread_id (the
    orchestrator fans out parallel `task` calls) must each get a distinct
    dispatch id so their heartbeat lines can be told apart in the logs."""
    caplog.set_level(logging.WARNING, logger="app.middleware.dynamic_tool_dispatch")

    upstream_a = _NeverYields()
    upstream_b = _NeverYields()

    async def drain(upstream):
        collected = []
        async for item in _iter_subagent_stream_with_stall_timeout(
            upstream,
            subagent_type="general-purpose",
            orchestrator_conversation_id="conv-same",
            subagent_thread_id="t-shared",
            tick_seconds=0.05,
            hard_cap_seconds=0.18,
        ):
            collected.append(item)
        return collected

    results = await asyncio.gather(drain(upstream_a), drain(upstream_b))
    id_a = results[0][0].data.metadata["dispatch_id"]
    id_b = results[1][0].data.metadata["dispatch_id"]
    assert id_a != id_b

    heartbeat_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "Still waiting on sub-agent" in r.getMessage()
    ]
    assert any(id_a in m for m in heartbeat_msgs)
    assert any(id_b in m for m in heartbeat_msgs)


@pytest.mark.asyncio
async def test_contextvar_set_reset_across_items_does_not_raise():
    """Regression: the real sub-agent stream sets a ContextVar at stream start
    and resets its ``contextvars.Token`` at stream end (graph_utils
    ``isolate_parent_stream_context`` / ``denest_parent_pregel_context`` and
    ``attachments_store`` ``current_attachments_backend``). A Token is bound to
    the exact context that ran ``set()`` and may only be ``reset()`` from that
    same context.

    Driving ``__anext__`` from a fresh ``asyncio`` task per item gives each item
    a *copied* context, so ``reset(token)`` runs in a different context and
    raises ``ValueError: <Token ...> was created in a different Context`` on the
    first real dispatch. The wrapper must consume the stream inside a single
    task so every ``set()``/``reset()`` pair shares one context.
    """
    cvar: contextvars.ContextVar = contextvars.ContextVar("subagent_stream_cvar")

    async def upstream():
        # set() on entry (first __anext__), reset() in finally (last __anext__).
        token = cvar.set("active")
        try:
            for value in ("p", "q", "r"):
                yield value
        finally:
            cvar.reset(token)

    collected = []
    async for item in _iter_subagent_stream_with_stall_timeout(
        upstream(),
        subagent_type="ctx-agent",
        orchestrator_conversation_id="conv-ctx",
        subagent_thread_id="t-ctx",
        tick_seconds=1.0,
        hard_cap_seconds=5.0,
    ):
        collected.append(item)

    # No ValueError leaked from the cross-context Token reset, all items intact,
    # and no ErrorEvent (the stream finished cleanly).
    assert collected == ["p", "q", "r"]
