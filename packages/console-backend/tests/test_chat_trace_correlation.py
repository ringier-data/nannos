"""Trace correlation for chat turns.

Why this exists: a turn's ids ride in span *metadata*, which X-Ray does not
index, and a root segment exported only when the turn ends can be dropped
before it arrives. Both happened — one turn of a two-turn conversation had no
root segment in X-Ray at all, so its 76s duration was invisible and the trace
was unfindable by conversation id. The ``[TRACE]`` log lines are the durable
pairing of conversation/message id to trace id.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from app import _traced_chat_message, _xray_trace_id
from opentelemetry import trace as otel_trace


def _trace_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "[TRACE]" in r.getMessage()]


class _FakeSpanContext:
    def __init__(self, trace_id: int) -> None:
        self.trace_id = trace_id


class _FakeSpan:
    def __init__(self, trace_id: int) -> None:
        self._context = _FakeSpanContext(trace_id)

    def get_span_context(self) -> _FakeSpanContext:
        return self._context


def test_xray_trace_id_uses_the_dashed_form_xray_accepts():
    """X-Ray splits the 128-bit id into a 32-bit prefix and a 96-bit remainder.

    The dashed form is the only one ``batch-get-traces`` and the console take,
    so the log line must carry it ready to paste.
    """
    raw = "6a8fde7a7e4d9bb88fe13ed8a25b4793"
    assert _xray_trace_id(_FakeSpan(int(raw, 16))) == "1-6a8fde7a-7e4d9bb88fe13ed8a25b4793"


def test_xray_trace_id_is_none_without_a_recording_span():
    """No injected OTel agent (local dev, tests) means trace_id 0, not a fake id."""
    assert _xray_trace_id(_FakeSpan(0)) is None


@pytest.mark.asyncio
async def test_start_and_end_lines_pair_the_ids_with_the_trace(caplog):
    calls = []

    @_traced_chat_message
    async def handle_send_message(sid, json_data):
        calls.append((sid, json_data))
        return {"ok": True}

    payload = {
        "conversationId": "01a041fb-e5cd-7599-a974-009924580b57",
        "id": "01a041fd-0e61-70a9-b4b8-d754c2b0dc9d",
    }
    with caplog.at_level(logging.INFO):
        assert await handle_send_message("sid-1", payload) == {"ok": True}

    assert calls == [("sid-1", payload)]
    start, end = _trace_lines(caplog)
    for line in (start, end):
        assert "conversation=01a041fb-e5cd-7599-a974-009924580b57" in line
        assert "message=01a041fd-0e61-70a9-b4b8-d754c2b0dc9d" in line
        assert "send_message" in line
    assert "start" in start
    assert "outcome=ok" in end
    assert "duration_s=" in end


@pytest.mark.asyncio
async def test_start_line_is_emitted_before_the_handler_runs(caplog):
    """A turn that never finishes is the one worth finding.

    If the pairing were only logged at the end, a crashed or cancelled turn
    would leave nothing to search for.
    """
    seen: list[list[str]] = []

    @_traced_chat_message
    async def handle_send_message(sid, json_data):
        seen.append(_trace_lines(caplog))
        return None

    with caplog.at_level(logging.INFO):
        await handle_send_message("sid-1", {"conversationId": "c-1", "id": "m-1"})

    (lines_during_handler,) = seen
    assert len(lines_during_handler) == 1
    assert "start" in lines_during_handler[0]


@pytest.mark.asyncio
async def test_failure_is_labelled_and_re_raised(caplog):
    @_traced_chat_message
    async def handle_send_message(sid, json_data):
        raise RuntimeError("orchestrator unreachable")

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="orchestrator unreachable"):
            await handle_send_message("sid-1", {"conversationId": "c-1", "id": "m-1"})

    _, end = _trace_lines(caplog)
    assert "outcome=RuntimeError" in end


@pytest.mark.asyncio
async def test_cancellation_is_labelled_and_re_raised(caplog):
    """A cancelled turn (client gone, shutdown) is the common non-ok ending.

    CancelledError derives from BaseException, so an ``except Exception`` here
    would mislabel it as ``ok``.
    """

    @_traced_chat_message
    async def handle_send_message(sid, json_data):
        raise asyncio.CancelledError()

    with caplog.at_level(logging.INFO):
        with pytest.raises(asyncio.CancelledError):
            await handle_send_message("sid-1", {"conversationId": "c-1", "id": "m-1"})

    _, end = _trace_lines(caplog)
    assert "outcome=CancelledError" in end


@pytest.mark.asyncio
async def test_missing_ids_do_not_break_the_line(caplog):
    """A malformed payload is rejected downstream; the line must still parse."""

    @_traced_chat_message
    async def handle_send_message(sid, json_data):
        return None

    with caplog.at_level(logging.INFO):
        await handle_send_message("sid-1", {})

    start, _ = _trace_lines(caplog)
    assert "conversation=-" in start
    assert "message=-" in start


@pytest.mark.asyncio
async def test_turn_span_is_a_fresh_root(caplog):
    """Parenting to the socket connection would merge a whole session into one
    trace — the reason this decorator exists. Guard it."""
    tracer = otel_trace.get_tracer("test")
    captured: list[otel_trace.SpanContext] = []

    @_traced_chat_message
    async def handle_send_message(sid, json_data):
        captured.append(otel_trace.get_current_span().get_span_context())
        return None

    with tracer.start_as_current_span("socket-connection") as connection:
        connection_context = connection.get_span_context()
        with caplog.at_level(logging.INFO):
            await handle_send_message("sid-1", {"conversationId": "c-1", "id": "m-1"})

    (turn_context,) = captured
    # With no SDK provider both are the invalid zero context, which would make
    # this assertion vacuous; only compare when tracing is actually live.
    if connection_context.trace_id:
        assert turn_context.trace_id != connection_context.trace_id
