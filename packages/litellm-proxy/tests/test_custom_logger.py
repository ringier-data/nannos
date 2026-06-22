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

import httpx
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


class _StatusResponse:
    """A response whose raise_for_status() raises an httpx.HTTPStatusError for the given code."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self._request = httpx.Request("POST", "http://backend.test")

    def raise_for_status(self) -> None:
        resp = httpx.Response(self.status_code, request=self._request)
        raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=self._request, response=resp)


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


# --- Retry-with-backoff + dead-letter on flush failure ---


def test_is_retryable_classification():
    """5xx and the transient 4xx (408/429) / timeouts / connection errors retry; other 4xx
    and unknown shapes do not."""
    req = httpx.Request("POST", "http://x")

    def status(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError("", request=req, response=httpx.Response(code, request=req))

    assert cl._is_retryable(status(503))
    assert cl._is_retryable(status(500))
    # Transient 4xx: a throttled or server-timed-out ingest will succeed on a retry — dropping
    # these would lose billing records the backend was simply too busy to accept right now.
    assert cl._is_retryable(status(429))  # Too Many Requests
    assert cl._is_retryable(status(408))  # Request Timeout
    # Non-transient 4xx: bad token / rejected payload — never retried (would wedge the worker).
    assert not cl._is_retryable(status(400))
    assert not cl._is_retryable(status(401))
    assert cl._is_retryable(httpx.ConnectError("refused"))
    assert cl._is_retryable(httpx.ReadTimeout("slow"))
    assert not cl._is_retryable(RuntimeError("unknown"))


@pytest.mark.asyncio
async def test_retryable_failure_is_retried_then_succeeds(logger_env, monkeypatch):
    """A transient connection error is retried with backoff and the batch still delivers."""
    monkeypatch.setattr(cl, "_FLUSH_BACKOFF_BASE", 0)  # don't actually sleep
    calls = {"n": 0}

    async def flaky_post(url, json, headers):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return _FakeResponse()

    monkeypatch.setattr(logger_env, "post", flaky_post)
    logger = cl.NannosCostLogger()
    dead: list = []
    monkeypatch.setattr(logger, "_dead_letter", lambda batch, exc: dead.append(batch))

    await logger._flush([{"id": 1}])

    assert calls["n"] == 3  # failed twice, succeeded on the third attempt
    assert dead == []  # delivered → nothing dead-lettered


@pytest.mark.asyncio
async def test_non_retryable_status_is_not_retried_and_dead_letters(logger_env, monkeypatch):
    """A 4xx won't fix itself, so it is dead-lettered immediately without retrying."""
    monkeypatch.setattr(cl, "_FLUSH_BACKOFF_BASE", 0)
    calls = {"n": 0}

    async def bad_request_post(url, json, headers):
        calls["n"] += 1
        return _StatusResponse(400)

    monkeypatch.setattr(logger_env, "post", bad_request_post)
    logger = cl.NannosCostLogger()
    dead: list = []
    monkeypatch.setattr(logger, "_dead_letter", lambda batch, exc: dead.append(batch))

    await logger._flush([{"id": 1}])

    assert calls["n"] == 1  # single attempt, no retry on 4xx
    assert dead == [[{"id": 1}]]


@pytest.mark.asyncio
async def test_exhausted_retries_dead_letter(logger_env, monkeypatch):
    """A sustained outage retries the cap then dead-letters the batch (no silent loss)."""
    monkeypatch.setattr(cl, "_FLUSH_BACKOFF_BASE", 0)
    calls = {"n": 0}

    async def always_down(url, json, headers):
        calls["n"] += 1
        raise httpx.ConnectError("backend down")

    monkeypatch.setattr(logger_env, "post", always_down)
    logger = cl.NannosCostLogger()
    dead: list = []
    monkeypatch.setattr(logger, "_dead_letter", lambda batch, exc: dead.append(batch))

    await logger._flush([{"id": 1}, {"id": 2}])

    assert calls["n"] == cl._FLUSH_MAX_RETRIES + 1  # first attempt + the retries
    assert dead == [[{"id": 1}, {"id": 2}]]


@pytest.mark.asyncio
async def test_buffer_full_dead_letters_record(logger_env, monkeypatch):
    """When the bounded queue is full, the dropped record is dead-lettered, not lost."""
    logger = cl.NannosCostLogger()
    dead: list = []
    monkeypatch.setattr(logger, "_dead_letter", lambda batch, exc: dead.append(batch))
    # Full queue + a no-op worker spawn, so put_nowait raises QueueFull deterministically.
    logger._queue = asyncio.Queue(maxsize=1)
    logger._queue.put_nowait({"id": 0})
    monkeypatch.setattr(logger, "_ensure_worker", lambda: None)

    await logger.async_log_success_event({"id": 1}, None, None, None)

    assert dead == [[{"id": 1}]]


def test_dead_letter_emits_parseable_line(caplog):
    """Each dead-lettered record is logged as a JSON line under the [cost][dead-letter] marker."""
    import json as _json
    import logging as _logging

    logger = cl.NannosCostLogger()
    with caplog.at_level(_logging.ERROR, logger="nannos.litellm.custom_logger"):
        logger._dead_letter([{"id": 1, "user_sub": "u1"}], RuntimeError("boom"))

    lines = [r.getMessage() for r in caplog.records if "[cost][dead-letter]" in r.getMessage()]
    assert len(lines) == 1
    payload = _json.loads(lines[0].split("[cost][dead-letter]", 1)[1].strip())
    assert payload == {"id": 1, "user_sub": "u1"}


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


def _record_kwargs(**overrides):
    """Minimal kwargs for _build_record with a billable usage + user attribution."""
    kwargs = {
        "model": "bedrock/anthropic.claude-3-5-sonnet",
        "litellm_params": {"metadata": {"spend_logs_metadata": {"user_sub": "u1"}}},
    }
    kwargs.update(overrides)
    return kwargs


def test_provider_falls_back_to_deployment_prefix_when_unset():
    """No custom_llm_provider → derive it from the deployment id prefix so the record is priceable."""

    class _Resp:
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    rec = cl._build_record(_record_kwargs(), _Resp())
    assert rec is not None
    assert rec["provider"] == "bedrock"


def test_provider_uses_explicit_custom_llm_provider_when_present():
    class _Resp:
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    rec = cl._build_record(_record_kwargs(custom_llm_provider="anthropic"), _Resp())
    assert rec is not None
    assert rec["provider"] == "anthropic"


def test_estimate_counts_pretokenized_integer_input():
    # Pre-tokenized embedding input (list of token ids) counts one token per id, never $0.
    assert cl._estimate_text_token_units({"input": [[1, 2, 3, 4], [5, 6]]}) == 6


def test_estimate_text_input_still_uses_char_heuristic():
    assert cl._estimate_text_token_units({"input": "x" * 40}) == 10  # ceil(40 / 4)


def test_estimate_short_text_bills_at_least_one_token():
    # A 1-3 char input must not floor to 0 tokens and get billed $0 (ceil division).
    assert cl._estimate_text_token_units({"input": "ok"}) == 1
    assert cl._estimate_text_token_units({"input": "x"}) == 1
    assert cl._estimate_text_token_units({"input": "x" * 5}) == 2  # ceil(5 / 4)
    assert cl._estimate_text_token_units({"input": ""}) == 0  # genuinely empty → 0


def test_non_image_data_uri_is_billed_as_binary_input():
    # data:application/pdf / data:text are excluded from the char estimate; they must be
    # counted as a binary (input_images) part rather than billed nothing by both paths.
    assert cl._estimate_text_token_units({"input": ["data:application/pdf;base64,AAAA"]}) == 0
    assert cl._count_image_inputs({"input": ["data:application/pdf;base64,AAAA"]}) == 1
    assert cl._count_image_inputs({"input": ["data:text/plain;base64,AAAA"]}) == 1
    # image data URIs and gs:// still count (unchanged behavior).
    assert cl._count_image_inputs({"input": ["data:image/png;base64,AAAA", "gs://bucket/x"]}) == 2
