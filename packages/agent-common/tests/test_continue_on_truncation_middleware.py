"""Tests for ContinueOnTruncationMiddleware.

A reasoning turn can exhaust its output budget while thinking and get cut off
(``finish_reason == "length"``) before emitting content or a tool call. The middleware
detects that, discards the poisoned generation, and re-runs the model with a wrap-up nudge
and raised ``max_tokens`` — recovering the turn in place instead of letting it be laundered
into a fake "Task completed successfully".
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from agent_common.middleware.continue_on_truncation import (
    ContinueOnTruncationMiddleware,
    _is_truncated,
)


class _FakeRequest:
    """Minimal stand-in for ModelRequest: the middleware only touches these fields."""

    def __init__(self, messages=None, model_settings=None):
        self.messages = messages or [HumanMessage(content="do the thing")]
        self.model_settings = model_settings or {}

    def override(self, **overrides):
        new = _FakeRequest(
            messages=overrides.get("messages", self.messages),
            model_settings=overrides.get("model_settings", self.model_settings),
        )
        return new


def _truncated() -> ModelResponse:
    """A turn cut off mid-thinking: empty content, no tool call, finish_reason=length."""
    return ModelResponse(
        result=[AIMessage(content="", response_metadata={"finish_reason": "length"})]
    )


def _complete(text: str = "here is the answer") -> ModelResponse:
    return ModelResponse(
        result=[AIMessage(content=text, response_metadata={"finish_reason": "stop"})]
    )


def _with_tool_call() -> ModelResponse:
    msg = AIMessage(
        content="",
        response_metadata={"finish_reason": "length"},
        tool_calls=[{"name": "search", "args": {}, "id": "t1"}],
    )
    return ModelResponse(result=[msg])


class _Handler:
    """Stub model handler returning a scripted sequence of responses, recording requests."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[_FakeRequest] = []

    def __call__(self, request):
        self.requests.append(request)
        return self._responses.pop(0)


class _AsyncHandler(_Handler):
    async def __call__(self, request):  # type: ignore[override]
        self.requests.append(request)
        return self._responses.pop(0)


# --- _is_truncated -----------------------------------------------------------------


def test_is_truncated_detects_length_cutoff():
    assert _is_truncated(_truncated()) is True


def test_is_truncated_false_for_normal_completion():
    assert _is_truncated(_complete()) is False


def test_is_truncated_false_when_tool_call_present():
    # A length cutoff that still produced a tool call is a normal agentic continuation.
    assert _is_truncated(_with_tool_call()) is False


def test_is_truncated_false_when_structured_response_present():
    resp = _truncated()
    resp.structured_response = {"message": "done"}
    assert _is_truncated(resp) is False


# --- retry behavior ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovers_after_one_truncation():
    handler = _AsyncHandler([_truncated(), _complete("recovered answer")])
    mw = ContinueOnTruncationMiddleware(max_retries=2)

    resp = await mw.awrap_model_call(_FakeRequest(), handler)

    assert resp.result[0].content == "recovered answer"
    assert len(handler.requests) == 2  # original + one retry
    # The retry appended the wrap-up nudge and raised max_tokens.
    retry_req = handler.requests[1]
    assert isinstance(retry_req.messages[-1], HumanMessage)
    assert "cut off" in retry_req.messages[-1].content
    assert retry_req.model_settings.get("max_tokens")


@pytest.mark.asyncio
async def test_no_retry_when_first_response_complete():
    handler = _AsyncHandler([_complete()])
    mw = ContinueOnTruncationMiddleware(max_retries=2)

    resp = await mw.awrap_model_call(_FakeRequest(), handler)

    assert resp.result[0].content == "here is the answer"
    assert len(handler.requests) == 1  # no retry


@pytest.mark.asyncio
async def test_exhausts_retries_and_returns_last_truncated():
    handler = _AsyncHandler([_truncated(), _truncated(), _truncated()])
    mw = ContinueOnTruncationMiddleware(max_retries=2)

    resp = await mw.awrap_model_call(_FakeRequest(), handler)

    # All attempts truncated: original + 2 retries, and the (still truncated) response is
    # returned so the downstream guard can surface an honest failure.
    assert len(handler.requests) == 3
    assert _is_truncated(resp)


@pytest.mark.asyncio
async def test_tool_calling_turn_is_not_retried():
    handler = _AsyncHandler([_with_tool_call()])
    mw = ContinueOnTruncationMiddleware(max_retries=2)

    resp = await mw.awrap_model_call(_FakeRequest(), handler)

    assert len(handler.requests) == 1
    assert resp.result[0].tool_calls


def test_sync_recovers_after_one_truncation():
    handler = _Handler([_truncated(), _complete("sync recovered")])
    mw = ContinueOnTruncationMiddleware(max_retries=2)

    resp = mw.wrap_model_call(_FakeRequest(), handler)

    assert resp.result[0].content == "sync recovered"
    assert len(handler.requests) == 2
