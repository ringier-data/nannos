"""The registration probe: which harness request shapes a model accepts, and what the gateway
hook and the app do with the record.

What is pinned here is the contract between the three consumers (console-backend writes the
flags, the proxy hook and agent-common read them), not the wire bodies themselves — those
are plain dicts a reader can compare against a gateway log.
"""

import json

import pytest
from ringier_a2a_sdk import model_capabilities as mc


def _ok_response(**message):
    msg = {"role": "assistant", "content": "Sunny.", **message}
    return {"choices": [{"message": msg, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}}


def _sse(*chunks):
    return "\n".join(f"data: {json.dumps(c)}" for c in chunks) + "\ndata: [DONE]\n"


def _stream_ok():
    return _sse(
        {"choices": [{"delta": {"role": "assistant", "content": "Sun"}}]},
        {"choices": [{"delta": {"content": "ny."}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"completion_tokens": 3}},
    )


class _Gateway:
    """A fake gateway that rejects the shapes its ``reject`` predicate names and records
    every body it was sent."""

    def __init__(self, reject=None, thinking_blocks=False):
        self.reject = reject or (lambda body: None)
        self.thinking_blocks = thinking_blocks
        self.bodies: list[dict] = []

    async def __call__(self, body):
        self.bodies.append(body)
        reason = self.reject(body)
        if reason:
            raise mc.ProbeCallError(reason, status=400)
        if body.get("stream"):
            return _stream_ok()
        if body.get("reasoning_effort") == "low" and self.thinking_blocks:
            return _ok_response(
                content=None,
                thinking_blocks=[{"type": "thinking", "thinking": "hm", "signature": "sig"}],
                tool_calls=[
                    {"id": "call_x", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
                ],
            )
        if body.get("reasoning_effort") == "none":
            return {**_ok_response(), "usage": {"completion_tokens_details": {"reasoning_tokens": 0}}}
        return _ok_response()


# --- a model that accepts everything ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_permissive_model_records_every_capability_and_rejects_nothing():
    gw = _Gateway(thinking_blocks=True)
    report = await mc.probe_model("m", gw, supports_reasoning=True)
    assert report.rejected == []
    assert report.limitations == []
    assert report.capabilities == {
        mc.FORCED_TOOL_CHOICE: True,
        mc.RESPONSE_FORMAT: True,
        mc.THINKING_OFF: mc.THINKING_OFF_DISABLED,
        mc.THINKING_REPLAY: True,
    }


# --- the Claude 5.5 case: forced tool_choice, response_format and thinking:disabled 400 ----


def _claude_55(body):
    tc = body.get("tool_choice")
    if tc == "required" or isinstance(tc, dict):
        return 'tool_choice: type "tool" and "any" are not supported for this model'
    if body.get("response_format"):
        return 'tool_choice: type "tool" and "any" are not supported for this model'
    if (body.get("thinking") or {}).get("type") == "disabled":
        return '"thinking.type.disabled" is not supported. Use "thinking.type.between_tools"'
    return None


@pytest.mark.asyncio
async def test_routable_failures_are_recorded_not_rejected():
    """Sonnet 5.5 fails three shapes the harness can route around; registration must go
    through with the limitations on record, because that record is what the hook uses."""
    gw = _Gateway(reject=_claude_55, thinking_blocks=True)
    report = await mc.probe_model("sonnet-5-5", gw, supports_reasoning=True)
    assert report.rejected == []
    assert {r.shape for r in report.limitations} == {"forced_tool_choice", "named_tool_choice", "response_format"}
    assert report.capabilities[mc.FORCED_TOOL_CHOICE] is False
    assert report.capabilities[mc.RESPONSE_FORMAT] is False
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_BETWEEN_TOOLS
    assert report.capabilities[mc.THINKING_REPLAY] is True
    # The error the admin sees is the provider's reason, not a stack trace.
    forced = next(r for r in report.results if r.shape == "forced_tool_choice")
    assert "not supported" in forced.error


@pytest.mark.asyncio
async def test_thinking_off_tries_disabled_before_between_tools():
    """``disabled`` is the cheaper switch (no thinking at all); ``between_tools`` is the
    fallback for models that refuse it. The order is the record's meaning."""
    gw = _Gateway()
    report = await mc.probe_model("m", gw)
    switches = [b["thinking"]["type"] for b in gw.bodies if "thinking" in b]
    assert switches == ["disabled"]
    assert report.capabilities[mc.THINKING_OFF] == "disabled"

    gw = _Gateway(reject=lambda b: "no" if (b.get("thinking") or {}).get("type") == "disabled" else None)
    report = await mc.probe_model("m", gw)
    switches = [b["thinking"]["type"] for b in gw.bodies if "thinking" in b]
    assert switches == ["disabled", "between_tools"]
    assert report.capabilities[mc.THINKING_OFF] == "between_tools"


@pytest.mark.asyncio
async def test_no_explicit_switch_is_a_recorded_state_not_a_failure_to_register():
    """Gemini 3 refuses ``thinking`` next to ``reasoning_effort`` in any form. That is
    'send reasoning_effort: none alone', which the hook already does — so it is recorded as
    ``none`` and the model still registers."""
    gw = _Gateway(reject=lambda b: "Cannot specify both" if "thinking" in b else None)
    report = await mc.probe_model("gemini", gw)
    assert report.rejected == []
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_NONE
    off = next(r for r in report.results if r.shape == "thinking_off")
    assert off.ok is False and "Cannot specify both" in off.error


# --- unavoidable shapes refuse registration ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "predicate, shape",
    [
        (lambda b: "no tools" if b.get("tools") and not b.get("stream") and len(b["messages"]) == 1 and b.get("tool_choice") == "auto" and "reasoning_effort" not in b and "thinking" not in b else None, "tools_auto"),
        (lambda b: "no tool results" if any(m.get("role") == "tool" for m in b["messages"]) and "reasoning_effort" not in b else None, "tool_round_trip"),
        (lambda b: "no streaming" if b.get("stream") else None, "streaming_tools"),
    ],
)
async def test_an_unavoidable_shape_failing_rejects_the_model(predicate, shape):
    gw = _Gateway(reject=predicate)
    report = await mc.probe_model("m", gw)
    assert [r.shape for r in report.rejected] == [shape]
    # The routable shapes still ran, so the admin sees the whole picture at once.
    assert {r.shape for r in report.results} >= {"forced_tool_choice", "response_format", "thinking_off"}


# --- transient failures are inconclusive, never a verdict ---------------------------------


@pytest.mark.asyncio
async def test_a_transport_failure_is_inconclusive_not_a_rejection():
    """A gateway that cannot be reached says nothing about the model: no rejection, no
    limitation, no flag — the admin is told the probe could not measure."""

    async def boom(body):
        raise RuntimeError("connection reset")

    report = await mc.probe_model("m", boom)
    assert report.rejected == [] and report.limitations == []
    assert {r.shape for r in report.inconclusive_unavoidable} == {"tools_auto", "tool_round_trip", "streaming_tools"}
    assert report.capabilities == {}
    assert all("connection reset" in r.error for r in report.inconclusive)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 408, None])
async def test_a_transient_status_on_a_routable_shape_leaves_the_key_out(status):
    """A Bedrock 429 on `response_format` must not be written down as 'rejects
    response_format' — that record would keep the model out of the utility tiers."""

    def _reject(body):
        return "throttled" if body.get("response_format") else None

    class _Gw(_Gateway):
        async def __call__(self, body):
            if _reject(body):
                raise mc.ProbeCallError("throttled", status=status)
            return await super().__call__(body)

    report = await mc.probe_model("m", _Gw())
    assert mc.RESPONSE_FORMAT not in report.capabilities
    assert mc.FORCED_TOOL_CHOICE in report.capabilities  # the others were measured
    rf = next(r for r in report.results if r.shape == "response_format")
    assert rf.inconclusive and not rf.ok
    assert report.limitations == []


@pytest.mark.asyncio
async def test_a_transient_failure_on_a_thinking_switch_records_no_switch():
    """`disabled` timing out does not mean `between_tools` is the answer."""

    def _reject(body):
        return "timeout" if (body.get("thinking") or {}).get("type") == "disabled" else None

    class _Gw(_Gateway):
        async def __call__(self, body):
            if _reject(body):
                self.bodies.append(body)
                raise mc.ProbeCallError("timeout", status=None)
            return await super().__call__(body)

    gw = _Gw()
    report = await mc.probe_model("m", gw)
    assert mc.THINKING_OFF not in report.capabilities
    assert [b["thinking"]["type"] for b in gw.bodies if "thinking" in b] == ["disabled"]


@pytest.mark.asyncio
async def test_a_definite_4xx_is_still_a_verdict():
    class _Gw(_Gateway):
        async def __call__(self, body):
            if body.get("response_format"):
                raise mc.ProbeCallError("not supported", status=400)
            return await super().__call__(body)

    report = await mc.probe_model("m", _Gw())
    assert report.capabilities[mc.RESPONSE_FORMAT] is False


@pytest.mark.asyncio
async def test_the_budget_makes_unreached_shapes_inconclusive():
    gw = _Gateway()
    report = await mc.probe_model("m", gw, budget_seconds=-1)
    assert report.capabilities == {}
    assert len(report.inconclusive) == len(report.results)
    assert gw.bodies == []


# --- probe traffic is marked and never fails over ----------------------------------------


@pytest.mark.asyncio
async def test_every_probe_request_is_marked_and_pinned_to_the_alias():
    gw = _Gateway(thinking_blocks=True)
    await mc.probe_model("m", gw, supports_reasoning=True)
    assert gw.bodies
    for body in gw.bodies:
        assert body["metadata"] == {mc.PROBE_MARKER: True}
        assert body["disable_fallbacks"] is True


def test_is_probe_request_reads_either_metadata_bucket():
    assert mc.is_probe_request({}) is False
    assert mc.is_probe_request({"metadata": {"user_api_key": "x"}}) is False
    assert mc.is_probe_request({"metadata": {mc.PROBE_MARKER: True, "user_api_key": "x"}}) is True
    assert mc.is_probe_request({"litellm_metadata": {mc.PROBE_MARKER: True}}) is True


# --- thinking replay ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_replay_is_skipped_for_models_not_declared_to_think():
    gw = _Gateway(thinking_blocks=True)
    report = await mc.probe_model("m", gw, supports_reasoning=False)
    assert mc.THINKING_REPLAY not in report.capabilities
    assert not any(b.get("reasoning_effort") == "low" for b in gw.bodies)


@pytest.mark.asyncio
async def test_thinking_replay_sends_back_the_models_own_signed_block():
    gw = _Gateway(thinking_blocks=True)
    await mc.probe_model("m", gw, supports_reasoning=True)
    replay = next(b for b in gw.bodies if len(b["messages"]) == 3 and b.get("reasoning_effort") == "low")
    assistant = replay["messages"][1]
    assert assistant["thinking_blocks"] == [{"type": "thinking", "thinking": "hm", "signature": "sig"}]
    assert assistant["tool_calls"][0]["id"] == "call_x"
    assert replay["messages"][2] == {"role": "tool", "tool_call_id": "call_x", "content": replay["messages"][2]["content"]}


@pytest.mark.asyncio
async def test_a_thinking_model_that_returns_no_block_records_nothing_to_replay():
    gw = _Gateway(thinking_blocks=False)
    report = await mc.probe_model("m", gw, supports_reasoning=True)
    assert report.capabilities[mc.THINKING_REPLAY] is None
    assert next(r for r in report.results if r.shape == "thinking_replay").ok is True


# --- stream assembly ----------------------------------------------------------------------


def test_assemble_stream_joins_content_and_tool_call_arguments_by_index():
    text = _sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get_weather", "arguments": '{"ci'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ty": "Zurich"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"completion_tokens": 9}},
    )
    out = mc.assemble_stream(text)
    msg = out["choices"][0]["message"]
    assert msg["tool_calls"] == [{"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Zurich"}'}}]
    assert out["usage"] == {"completion_tokens": 9}
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_assemble_stream_surfaces_an_error_event():
    with pytest.raises(mc.ProbeCallError, match="quota"):
        mc.assemble_stream('data: {"error": {"message": "quota exceeded"}}\n')


# --- hook-side helpers --------------------------------------------------------------------


def test_capabilities_of_tolerates_unprobed_and_malformed_model_info():
    assert mc.capabilities_of(None) == {}
    assert mc.capabilities_of({}) == {}
    assert mc.capabilities_of({mc.CAPABILITIES_KEY: "nope"}) == {}
    assert mc.capabilities_of({mc.CAPABILITIES_KEY: {mc.FORCED_TOOL_CHOICE: False}}) == {mc.FORCED_TOOL_CHOICE: False}


@pytest.mark.parametrize(
    "tool_choice", ["required", "any", {"type": "function", "function": {"name": "x"}}, {"type": "tool", "name": "x"}]
)
def test_forced_tool_choice_is_downgraded_only_when_recorded_unsupported(tool_choice):
    kwargs = {"tool_choice": tool_choice}
    assert mc.downgrade_forced_tool_choice(kwargs, {}) is False  # unprobed: no opinion
    assert kwargs["tool_choice"] == tool_choice
    assert mc.downgrade_forced_tool_choice(kwargs, {mc.FORCED_TOOL_CHOICE: True}) is False
    assert mc.downgrade_forced_tool_choice(kwargs, {mc.FORCED_TOOL_CHOICE: False}) is True
    assert kwargs["tool_choice"] == "auto"


@pytest.mark.parametrize("tool_choice", ["auto", "none", None])
def test_an_unforced_tool_choice_is_never_touched(tool_choice):
    kwargs = {"tool_choice": tool_choice}
    assert mc.downgrade_forced_tool_choice(kwargs, {mc.FORCED_TOOL_CHOICE: False}) is False
    assert kwargs["tool_choice"] == tool_choice


def test_thinking_off_switch_follows_the_record():
    assert mc.thinking_off_switch({}) is None
    assert mc.thinking_off_switch({mc.THINKING_OFF: "none"}) is None
    assert mc.thinking_off_switch({mc.THINKING_OFF: "disabled"}) == {"type": "disabled"}
    assert mc.thinking_off_switch({mc.THINKING_OFF: "between_tools"}) == {"type": "between_tools"}


@pytest.mark.asyncio
async def test_every_routable_shape_maps_to_a_record_key():
    """A merge over a stored record keeps the keys of inconclusive shapes; a routable shape
    the map does not know would have its earlier flag silently erased."""

    async def transient(body):
        raise mc.ProbeCallError("throttled", status=429)

    report = await mc.probe_model("m", transient, supports_reasoning=True)
    routable = {r.shape for r in report.inconclusive if not r.unavoidable}
    assert routable == set(mc.SHAPE_KEYS)
    assert report.inconclusive_keys == {mc.FORCED_TOOL_CHOICE, mc.RESPONSE_FORMAT, mc.THINKING_OFF, mc.THINKING_REPLAY}
