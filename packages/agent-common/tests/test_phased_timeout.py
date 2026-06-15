"""Tests for the two-phase streaming stall timeout (phased_timeout).

Drives the wrapper against a plain fake model with a scriptable `_astream`, so
the tests need no real provider/network and don't depend on langchain imports.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agent_common.core.phased_timeout import StreamStalledError, with_phased_stream_timeout


class _FakeModel:
    """Minimal stand-in for a BaseChatModel exposing a scriptable async `_astream`.

    `scripts` is a list (one entry per stream attempt). Each script is a list of
    steps: a float means "await sleep(t) before the next event" (used to trigger a
    timeout), any other value is yielded as a chunk.
    """

    def __init__(self, scripts: list[list]):
        self._scripts = scripts
        self.attempts = 0

    async def _astream(self, *args, **kwargs):
        script = self._scripts[self.attempts]
        self.attempts += 1
        for step in script:
            if isinstance(step, float):
                await asyncio.sleep(step)
            else:
                yield step


def _wrap(scripts, **cfg):
    cls = with_phased_stream_timeout(
        _FakeModel,
        first_token_timeout=cfg.get("ftt", 0.10),
        inter_chunk_timeout=cfg.get("ict", 0.05),
        first_token_retries=cfg.get("ftr", 2),
    )
    model = cls(scripts)
    return model


async def _collect(model):
    return [c async for c in model._astream()]


@pytest.mark.asyncio
async def test_healthy_stream_passes_all_chunks():
    model = _wrap([["a", "b", "c"]])
    assert await _collect(model) == ["a", "b", "c"]
    assert model.attempts == 1


@pytest.mark.asyncio
async def test_stall_before_first_token_retries_then_succeeds(caplog):
    caplog.set_level(logging.WARNING, logger="agent_common.core.phased_timeout")
    # Attempt 1 stalls past the first-token timeout (0.10s) before yielding;
    # attempt 2 streams cleanly.
    model = _wrap([[0.30, "late"], ["x", "y"]], ftt=0.10, ict=0.05)
    assert await _collect(model) == ["x", "y"]
    assert model.attempts == 2
    assert any("produced no first token" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_stall_before_first_token_exhausts_retries_and_raises():
    # Every attempt stalls before the first token; with ftr=2 that's 3 attempts.
    model = _wrap([[0.30, "a"], [0.30, "a"], [0.30, "a"]], ftt=0.08, ftr=2)
    with pytest.raises(StreamStalledError, match="first-token phase"):
        await _collect(model)
    assert model.attempts == 3


@pytest.mark.asyncio
async def test_stall_mid_stream_does_not_retry_and_raises():
    # First token arrives, then the stream stalls past the inter-chunk timeout.
    # Mid-stream stalls are not retryable (partial output already emitted).
    model = _wrap([["a", 0.30, "b"]], ftt=0.20, ict=0.05, ftr=2)
    collected = []
    with pytest.raises(StreamStalledError, match="inter-chunk phase"):
        async for c in model._astream():
            collected.append(c)
    assert collected == ["a"]
    assert model.attempts == 1


@pytest.mark.asyncio
async def test_first_token_allowed_to_be_slow_within_budget():
    # A first token that arrives just under the generous first-token budget is fine,
    # even though it far exceeds the tight inter-chunk budget.
    model = _wrap([[0.08, "a", "b"]], ftt=0.20, ict=0.05)
    assert await _collect(model) == ["a", "b"]
    assert model.attempts == 1
