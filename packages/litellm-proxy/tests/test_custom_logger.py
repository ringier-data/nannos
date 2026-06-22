"""Unit tests for the gateway cost logger's batching/hot-path behavior.

The litellm-proxy image is built from upstream LiteLLM (no local venv), so we stub the
``litellm`` import and exercise NannosCostLogger directly. Run via a venv that has
pytest-asyncio + httpx, e.g.:

    uv run --project ../agent-common pytest tests/test_custom_logger.py
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

# Stub the litellm base class so custom_logger imports without litellm installed.
_litellm = types.ModuleType("litellm")
_integrations = types.ModuleType("litellm.integrations")
_cl_mod = types.ModuleType("litellm.integrations.custom_logger")


class _CustomLoggerBase:  # minimal stand-in for litellm.integrations.custom_logger.CustomLogger
    pass


_cl_mod.CustomLogger = _CustomLoggerBase
sys.modules.setdefault("litellm", _litellm)
sys.modules.setdefault("litellm.integrations", _integrations)
sys.modules.setdefault("litellm.integrations.custom_logger", _cl_mod)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import custom_logger as cl  # noqa: E402


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass


class _FakeClient:
    """Records every POST payload; counts how many times it was instantiated."""

    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self.posts: list[dict] = []

    async def post(self, url, json, headers):
        self.posts.append(json)
        return _FakeResponse()


@pytest.fixture
def logger_env(monkeypatch):
    monkeypatch.setattr(cl, "CONSOLE_BACKEND_URL", "http://backend.test")
    monkeypatch.setattr(cl, "_build_record", lambda kwargs, resp: {"id": kwargs["id"]})
    _FakeClient.instances = 0
    client = _FakeClient()
    monkeypatch.setattr(cl, "_get_client", lambda: client)
    # Reset the module-level shared client so _get_client isn't bypassed by a prior test.
    monkeypatch.setattr(cl, "_client", None)
    return client


async def _drain(logger: "cl.NannosCostLogger") -> None:
    assert logger._queue is not None
    await asyncio.wait_for(logger._queue.join(), timeout=2.0)


@pytest.mark.asyncio
async def test_records_are_not_posted_on_the_hot_path(logger_env):
    """async_log_success_event must enqueue and return without awaiting the POST."""
    logger = cl.NannosCostLogger()
    for i in range(5):
        await logger.async_log_success_event({"id": i}, None, None, None)
    # Worker hasn't been given the loop yet → nothing has been POSTed.
    assert logger_env.posts == []
    await _drain(logger)
    assert sum(len(p["logs"]) for p in logger_env.posts) == 5


@pytest.mark.asyncio
async def test_records_are_batched_and_delivered_once(logger_env):
    logger = cl.NannosCostLogger()
    for i in range(250):
        await logger.async_log_success_event({"id": i}, None, None, None)
    await _drain(logger)

    delivered = [r["id"] for p in logger_env.posts for r in p["logs"]]
    assert sorted(delivered) == list(range(250))  # every record exactly once
    assert len(logger_env.posts) < 250  # coalesced into fewer POSTs than records
    assert all(len(p["logs"]) <= cl._FLUSH_MAX_BATCH for p in logger_env.posts)


@pytest.mark.asyncio
async def test_single_client_reused_across_calls(logger_env):
    logger = cl.NannosCostLogger()
    for i in range(20):
        await logger.async_log_success_event({"id": i}, None, None, None)
    await _drain(logger)
    # The fixture pre-builds one client; the logger must reuse it, never build its own.
    assert _FakeClient.instances == 1


@pytest.mark.asyncio
async def test_flush_failure_does_not_kill_worker(logger_env, monkeypatch):
    """A POST failure is logged but the worker keeps draining subsequent records."""
    calls = {"n": 0}

    async def flaky_post(url, json, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("backend down")
        return _FakeResponse()

    monkeypatch.setattr(logger_env, "post", flaky_post)

    logger = cl.NannosCostLogger()
    await logger.async_log_success_event({"id": 1}, None, None, None)
    await _drain(logger)
    # Worker survived the first failed flush; enqueue more and confirm they still flush.
    await logger.async_log_success_event({"id": 2}, None, None, None)
    await _drain(logger)
    assert calls["n"] >= 2


# --- Billing breakdown: embedding token-counting edge cases (#7) ---

def test_total_tokens_only_billed_as_input():
    """An embedding provider that reports ONLY total_tokens bills real tokens, not $0/estimate."""
    bd = cl._billing_unit_breakdown({"total_tokens": 1234})
    assert bd == {"base_input_tokens": 1234}


def test_chat_total_tokens_not_miscounted_as_input():
    """A chat call with a normal split is unaffected (total_tokens fallback must not trigger)."""
    bd = cl._billing_unit_breakdown({"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140})
    assert bd["base_input_tokens"] == 100
    assert bd["base_output_tokens"] == 40


def test_output_only_does_not_borrow_total_as_input():
    # If only output + total are present (degenerate), the input fallback stays off.
    bd = cl._billing_unit_breakdown({"completion_tokens": 50, "total_tokens": 50})
    assert "base_input_tokens" not in bd
    assert bd["base_output_tokens"] == 50


def test_estimate_counts_pretokenized_integer_input():
    # Pre-tokenized embedding input (list of token ids) counts one token per id, never $0.
    assert cl._estimate_text_token_units({"input": [[1, 2, 3, 4], [5, 6]]}) == 6


def test_estimate_text_input_still_uses_char_heuristic():
    assert cl._estimate_text_token_units({"input": "x" * 40}) == 10  # 40 // 4
