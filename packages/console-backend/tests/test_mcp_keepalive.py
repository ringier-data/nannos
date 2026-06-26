"""Tests for the MCP progress-notification keepalive (utils/mcp_keepalive)."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from console_backend.utils.mcp_keepalive import with_progress_keepalive


def _make_server(*, progress_token, session):
    ctx = SimpleNamespace(
        meta=SimpleNamespace(progressToken=progress_token) if progress_token is not None else None,
        session=session,
        request_id="req-1",
    )
    return SimpleNamespace(request_context=ctx)


@pytest.mark.asyncio
async def test_keepalive_emits_notifications_during_slow_call_and_returns_result():
    session = AsyncMock()

    async def slow():
        await asyncio.sleep(0.25)
        return "done"

    server = _make_server(progress_token="tok-123", session=session)
    result = await with_progress_keepalive(slow(), server, interval=0.05)

    assert result == "done"
    # With token: both progress + log keepalives fire on each tick.
    assert session.send_progress_notification.await_count >= 2
    assert session.send_log_message.await_count >= 2
    # Progress is monotonic and stays below total (asymptotic curve).
    progresses = [c.kwargs["progress"] for c in session.send_progress_notification.await_args_list]
    assert progresses == sorted(progresses)
    assert all(p < 1.0 for p in progresses)


@pytest.mark.asyncio
async def test_keepalive_log_message_without_token():
    session = AsyncMock()

    async def slow():
        await asyncio.sleep(0.15)
        return 42

    server = _make_server(progress_token=None, session=session)
    result = await with_progress_keepalive(slow(), server, interval=0.05)

    assert result == 42
    # No token → no progress notifications, but log keepalives still fire.
    session.send_progress_notification.assert_not_awaited()
    assert session.send_log_message.await_count >= 1


@pytest.mark.asyncio
async def test_fast_call_sends_no_keepalive():
    session = AsyncMock()

    async def fast():
        return "quick"

    server = _make_server(progress_token="tok", session=session)
    result = await with_progress_keepalive(fast(), server, interval=0.05)

    assert result == "quick"
    session.send_progress_notification.assert_not_awaited()
    session.send_log_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_exception_propagates_unchanged():
    session = AsyncMock()

    async def boom():
        await asyncio.sleep(0.12)
        raise ValueError("tool failed")

    server = _make_server(progress_token="tok", session=session)
    with pytest.raises(ValueError, match="tool failed"):
        await with_progress_keepalive(boom(), server, interval=0.05)


@pytest.mark.asyncio
async def test_no_request_context_awaits_without_keepalive():
    class _NoCtx:
        @property
        def request_context(self):
            raise LookupError("no context")

    async def work():
        return "ok"

    result = await with_progress_keepalive(work(), _NoCtx(), interval=0.05)
    assert result == "ok"


@pytest.mark.asyncio
async def test_failed_keepalive_does_not_break_tool():
    session = AsyncMock()
    session.send_log_message.side_effect = RuntimeError("stream gone")
    session.send_progress_notification.side_effect = RuntimeError("stream gone")

    async def slow():
        await asyncio.sleep(0.15)
        return "survived"

    server = _make_server(progress_token="tok", session=session)
    result = await with_progress_keepalive(slow(), server, interval=0.05)
    assert result == "survived"


@pytest.mark.asyncio
async def test_outer_cancellation_cancels_underlying_tool_task():
    """If the /mcp handler is cancelled mid-call, the shielded tool task must not
    be left running detached — the keepalive cancels it on the way out."""
    session = AsyncMock()
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
            return "should not finish"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    server = _make_server(progress_token="tok", session=session)
    outer = asyncio.ensure_future(with_progress_keepalive(slow(), server, interval=0.05))
    # Let the keepalive loop tick at least once, then cancel the outer wrapper.
    await asyncio.sleep(0.12)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    # The underlying tool task must have received the cancellation (no orphan leak).
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
