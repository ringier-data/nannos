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


_WEATHER_CALL = {"id": "call_x", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}


def _reasoning(tokens):
    return {**_ok_response(content="21"), "usage": {"completion_tokens_details": {"reasoning_tokens": tokens}}}


class _Gateway:
    """A fake gateway in front of a model that honours what it accepts: a forced tool_choice
    yields a tool call, response_format yields the schema, thinking on yields reasoning and a
    thinking-off switch yields none. It rejects the shapes its ``reject`` predicate names and
    records every body it was sent.

    ``rewrites`` plays LiteLLM's ``drop_params``: ``"forced"`` answers a forced tool_choice as
    ``auto`` would (the prompt forbids the tool, so no call), ``"thinking_disabled"`` drops
    ``thinking: disabled`` so the model reasons anyway — both with a 200. ``reasons=False`` is
    a model that shows no reasoning even with thinking on; ``floor=True`` one whose
    ``reasoning_effort: none`` still reasons (Gemini 3 maps it to thinkingLevel minimal)."""

    def __init__(self, reject=None, thinking_blocks=False, rewrites=(), reasons=True, floor=False):
        self.reject = reject or (lambda body: None)
        self.thinking_blocks = thinking_blocks
        self.rewrites = set(rewrites)
        self.reasons = reasons
        self.floor = floor
        self.bodies: list[dict] = []

    async def __call__(self, body):
        self.bodies.append(body)
        reason = self.reject(body)
        if reason:
            raise mc.ProbeCallError(reason, status=400)
        if body.get("stream"):
            return _stream_ok()
        effort = body.get("reasoning_effort")
        if effort == "low" and self.thinking_blocks:
            return _ok_response(
                content=None,
                thinking_blocks=[{"type": "thinking", "thinking": "hm", "signature": "sig"}],
                tool_calls=[_WEATHER_CALL],
            )
        if effort == "high":
            return _reasoning(250 if self.reasons else 0)
        if effort == "none":
            switch = (body.get("thinking") or {}).get("type")
            dropped = switch == "disabled" and "thinking_disabled" in self.rewrites
            return _reasoning(180 if dropped or (switch is None and self.floor) else 0)
        tc = body.get("tool_choice")
        if (tc == "required" or isinstance(tc, dict)) and "forced" not in self.rewrites:
            return _ok_response(content=None, tool_calls=[_WEATHER_CALL])
        if body.get("response_format"):
            return _ok_response(content='{"answer": "yes", "confidence": 0.99}')
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
async def test_a_gateway_rewrite_is_graded_as_the_rejection_it_hides():
    """The same Sonnet 5.5 behind a LiteLLM whose map flags it: forced tool_choice is
    downgraded to auto and thinking:disabled dropped, every call answers 200. Grading on the
    status alone recorded the opposite of the truth (live QA, 2026-09-30); the reply shows it."""
    gw = _Gateway(rewrites={"forced", "thinking_disabled"}, thinking_blocks=True)
    report = await mc.probe_model("sonnet-5-5", gw, supports_reasoning=True)
    assert report.rejected == []
    assert report.capabilities[mc.FORCED_TOOL_CHOICE] is False
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_BETWEEN_TOOLS
    forced = next(r for r in report.results if r.shape == "forced_tool_choice")
    assert not forced.ok and not forced.inconclusive
    assert "no tool call" in forced.error and "rewritten" in forced.error
    assert [b["thinking"]["type"] for b in gw.bodies if "thinking" in b] == ["disabled", "between_tools"]


@pytest.mark.asyncio
async def test_a_named_tool_choice_answered_with_another_tool_is_not_honoured():
    class _Gw(_Gateway):
        async def __call__(self, body):
            if isinstance(body.get("tool_choice"), dict):
                self.bodies.append(body)
                return _ok_response(content=None, tool_calls=[{**_WEATHER_CALL, "function": {"name": "other", "arguments": "{}"}}])
            return await super().__call__(body)

    report = await mc.probe_model("m", _Gw())
    assert report.capabilities[mc.FORCED_TOOL_CHOICE] is False
    assert "['other']" in next(r for r in report.results if r.shape == "named_tool_choice").error


@pytest.mark.asyncio
async def test_a_response_format_reply_that_is_not_the_schema_is_a_rejection():
    class _Gw(_Gateway):
        async def __call__(self, body):
            if body.get("response_format"):
                self.bodies.append(body)
                return _ok_response(content="Yes, Zurich is in Switzerland.")
            return await super().__call__(body)

    report = await mc.probe_model("m", _Gw())
    assert report.capabilities[mc.RESPONSE_FORMAT] is False
    rf = next(r for r in report.results if r.shape == "response_format")
    assert not rf.inconclusive and "not JSON matching the schema" in rf.error


@pytest.mark.asyncio
async def test_a_reply_cut_off_before_it_can_be_judged_is_inconclusive():
    """No tool call because max_tokens ran out is not evidence the force was rewritten."""

    class _Gw(_Gateway):
        async def __call__(self, body):
            if body.get("tool_choice") == "required":
                self.bodies.append(body)
                return {"choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "length"}]}
            return await super().__call__(body)

    report = await mc.probe_model("m", _Gw())
    assert mc.FORCED_TOOL_CHOICE not in report.capabilities
    assert next(r for r in report.results if r.shape == "forced_tool_choice").inconclusive


@pytest.mark.asyncio
async def test_a_clean_switch_is_verified_against_a_control_turn_once():
    gw = _Gateway()
    report = await mc.probe_model("m", gw)
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_DISABLED
    assert len([b for b in gw.bodies if b.get("reasoning_effort") == "high"]) == 1
    assert "unverified" not in next(r for r in report.results if r.shape == "thinking_off").note


@pytest.mark.asyncio
async def test_a_switch_is_recorded_but_unverified_when_the_control_does_not_reason():
    """A model that shows no reasoning even with thinking on gives the grade nothing to go on:
    the switch was accepted and nothing contradicts it, so it is kept — and said to be unchecked."""
    report = await mc.probe_model("m", _Gateway(reasons=False))
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_DISABLED
    assert "unverified" in next(r for r in report.results if r.shape == "thinking_off").note


_GEMINI_3 = lambda b: "Cannot specify both `thinking` and `thinking_level`" if "thinking" in b else None  # noqa: E731


@pytest.mark.asyncio
async def test_a_model_whose_effort_alone_still_reasons_is_always_on():
    """Gemini 3: no explicit switch is taken, and `reasoning_effort: none` alone becomes
    thinkingLevel minimal — it still reasons. Nothing turns thinking off, which is what the
    console needs to know to stop offering the toggle."""
    gw = _Gateway(reject=_GEMINI_3, floor=True)
    report = await mc.probe_model("gemini", gw)
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_ALWAYS_ON
    assert mc.thinking_always_on(report.capabilities) is True
    off = next(r for r in report.results if r.shape == "thinking_off")
    assert not off.ok and not off.inconclusive and "cannot be turned off" in off.error
    # A reasoning reply is its own evidence: no control turn is needed.
    assert not any(b.get("reasoning_effort") == "high" for b in gw.bodies)
    # The floor is measured, not assumed: the lowest effort it accepts becomes the record.
    assert report.capabilities[mc.THINKING_FLOOR] == "minimal"
    floor = next(r for r in report.results if r.shape == "thinking_floor")
    assert floor.ok and "minimal" in floor.note


@pytest.mark.asyncio
async def test_an_always_on_floor_is_the_lowest_effort_accepted():
    """Claude Opus 5.5: both switches 400, `none` alone still reasons (LiteLLM sends no effort,
    so it runs at the DEFAULT one — nannos#330). The first candidate it accepts is the floor."""
    refuse = lambda b: (
        "not supported for this model" if "thinking" in b or b.get("reasoning_effort") == "minimal" else None
    )
    report = await mc.probe_model("opus", _Gateway(reject=refuse, floor=True))
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_ALWAYS_ON
    assert report.capabilities[mc.THINKING_FLOOR] == "low"


@pytest.mark.asyncio
async def test_the_floor_is_tried_from_the_deployments_declared_levels():
    """The console passes the levels its picker offers: the floor is the lowest of them that is
    accepted, so it is always a level the admin also sees. Only the lowest two are tried."""
    refuse = lambda b: "not supported" if "thinking" in b or b.get("reasoning_effort") == "low" else None
    gw = _Gateway(reject=refuse, floor=True)
    report = await mc.probe_model("opus", gw, floor_candidates=["low", "medium", "high"])
    assert report.capabilities[mc.THINKING_FLOOR] == "medium"
    tried = [b["reasoning_effort"] for b in gw.bodies if b.get("reasoning_effort") not in ("none", "high", None)]
    assert "minimal" not in tried  # not a declared level, so never tried

    refuse_all = lambda b: "not supported" if "thinking" in b or b.get("reasoning_effort") in ("low", "medium") else None
    gw = _Gateway(reject=refuse_all, floor=True)
    report = await mc.probe_model("opus", gw, floor_candidates=["low", "medium", "high"])
    assert mc.THINKING_FLOOR not in report.capabilities
    assert not any(b.get("reasoning_effort") == "high" and b.get("max_tokens") == 1024 for b in gw.bodies)


@pytest.mark.asyncio
async def test_no_declared_levels_falls_back_to_the_fixed_pair():
    gw = _Gateway(reject=_GEMINI_3, floor=True)
    report = await mc.probe_model("gemini", gw, floor_candidates=[])
    assert report.capabilities[mc.THINKING_FLOOR] == "minimal"


@pytest.mark.asyncio
async def test_an_always_on_model_that_refuses_every_level_records_no_floor():
    """A deployment the gateway's model map does not translate (a Bedrock ARN: every effort
    becomes a `budget_tokens` the model rejects) has no floor to send; it is a limitation the
    admin sees, and the gateway keeps sending `none` alone."""
    refuse = lambda b: (
        "budget_tokens is not supported" if "thinking" in b or b.get("reasoning_effort") in ("minimal", "low") else None
    )
    report = await mc.probe_model("arn", _Gateway(reject=refuse, floor=True))
    assert mc.THINKING_FLOOR not in report.capabilities
    floor = next(r for r in report.results if r.shape == "thinking_floor")
    assert not floor.ok and not floor.inconclusive and "no thinking level is accepted" in floor.error
    assert floor in report.limitations


@pytest.mark.asyncio
async def test_a_transient_floor_attempt_keeps_the_stored_floor():
    gw = _Gateway(reject=_GEMINI_3, floor=True)

    async def call(body):
        if body.get("reasoning_effort") == "minimal":
            raise mc.ProbeCallError("throttled", status=429)
        return await gw(body)

    report = await mc.probe_model("gemini", call)
    assert mc.THINKING_FLOOR not in report.capabilities
    assert mc.THINKING_FLOOR in report.inconclusive_keys


@pytest.mark.asyncio
async def test_no_floor_is_measured_where_thinking_turns_off():
    gw = _Gateway()
    report = await mc.probe_model("m", gw)
    assert mc.THINKING_FLOOR not in report.capabilities
    floor = next(r for r in report.results if r.shape == "thinking_floor")
    assert floor.ok and "not needed" in floor.note
    assert not any(b.get("reasoning_effort") in mc.THINKING_FLOOR_CANDIDATES for b in gw.bodies if not b.get("thinking"))


@pytest.mark.asyncio
async def test_a_refused_effort_is_recorded_as_no_switch_with_the_reason():
    gw = _Gateway(reject=lambda b: "no reasoning_effort none" if b.get("reasoning_effort") == "none" else None)
    report = await mc.probe_model("m", gw)
    # Not ``none`` — that means the effort alone works, and the hook would keep sending the
    # request the probe just saw refused (review round 7).
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_UNSUPPORTED
    assert mc.thinking_off_sendable(report.capabilities) is False
    off = next(r for r in report.results if r.shape == "thinking_off")
    assert not off.ok and "no reasoning_effort none" in off.error


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
async def test_the_effort_alone_is_recorded_when_it_turns_thinking_off():
    """A model that refuses `thinking` next to `reasoning_effort` in any form but whose
    `reasoning_effort: none` alone does turn thinking off: recorded as ``none`` — a way that
    works, not a limitation — and still registers."""
    gw = _Gateway(reject=_GEMINI_3)
    report = await mc.probe_model("gemini", gw)
    assert report.rejected == [] and report.limitations == []
    assert report.capabilities[mc.THINKING_OFF] == mc.THINKING_OFF_NONE
    assert mc.thinking_always_on(report.capabilities) is False
    off = next(r for r in report.results if r.shape == "thinking_off")
    assert off.ok and "unverified" not in off.note
    efforts_alone = [b for b in gw.bodies if b.get("reasoning_effort") == "none" and "thinking" not in b]
    assert len(efforts_alone) == 1


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


@pytest.mark.asyncio
async def test_a_cooled_down_deployment_stops_the_probe_instead_of_repeating_the_cooldown():
    """A 404 on the first shape makes LiteLLM cool the deployment down; every later request
    would come back "No deployments available". The probe stops asking and says why once."""
    sent: list[dict] = []

    async def gateway(body):
        sent.append(body)
        if len(sent) == 1:
            raise mc.ProbeCallError("Publisher model was not found", status=404)
        raise mc.ProbeCallError("No deployments available for selected model, cooldown_list=['d']", status=429)

    report = await mc.probe_model("m", gateway, supports_reasoning=True)
    assert len(sent) == 2
    assert [r.shape for r in report.rejected] == ["tools_auto"]
    rest = [r for r in report.results if r.shape != "tools_auto"]
    assert rest and all(r.inconclusive and "not probed" in r.error for r in rest)
    assert report.capabilities == {}


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


# --- progress ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_announces_every_request_and_every_verdict_in_order():
    events: list[dict] = []
    gw = _Gateway(thinking_blocks=True)
    report = await mc.probe_model("m", gw, supports_reasoning=True, on_progress=events.append)
    steps = [e for e in events if e["type"] == "step"]
    results = [e for e in events if e["type"] == "result"]
    assert len(steps) == len(gw.bodies)  # one announcement per request, before it is sent
    assert [e["shape"] for e in results] == [r.shape for r in report.results]
    assert [e["shape"] for e in results] == mc.planned_shapes(supports_reasoning=True)
    assert all(e["label"] for e in steps + results)
    assert {"thinking_off:disabled", "thinking_off:control"} <= {e["step"] for e in steps}


@pytest.mark.asyncio
async def test_a_failing_progress_callback_never_changes_the_verdict():
    async def broken(event):
        raise RuntimeError("socket closed")

    assert (await mc.probe_model("m", _Gateway(), on_progress=broken)).capabilities == (
        await mc.probe_model("m", _Gateway())
    ).capabilities


def test_planned_shapes_leave_out_replay_for_models_not_declared_to_think():
    assert "thinking_replay" not in mc.planned_shapes(supports_reasoning=False)
    assert mc.planned_shapes(supports_reasoning=True)[-1] == "thinking_replay"


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


def test_thinking_off_is_sendable_only_where_the_record_says_how_it_goes_off():
    assert mc.thinking_off_sendable({}) is False  # unprobed: no opinion, send no effort
    assert mc.thinking_off_sendable({mc.THINKING_OFF: mc.THINKING_OFF_UNSUPPORTED}) is False
    for way in ("disabled", "between_tools", "none", "always_on"):
        assert mc.thinking_off_sendable({mc.THINKING_OFF: way}) is True


def _off(**extra):
    return {"model": "m", "reasoning_effort": "none", **extra}


@pytest.mark.parametrize(
    "caps, effort, thinking",
    [
        ({mc.THINKING_OFF: "disabled"}, "none", {"type": "disabled"}),
        ({mc.THINKING_OFF: "between_tools"}, "none", {"type": "between_tools"}),
        ({mc.THINKING_OFF: "none"}, "none", None),
        ({mc.THINKING_OFF: "always_on", mc.THINKING_FLOOR: "low"}, "low", None),
        # Probed before the floor was measured, or no level was accepted: the effort alone.
        ({mc.THINKING_OFF: "always_on"}, "none", None),
        ({mc.THINKING_OFF: "unsupported"}, None, None),
        # Unprobed: nothing is guessed from the model's name.
        ({}, "none", None),
    ],
)
def test_apply_thinking_off_follows_the_record(caps, effort, thinking):
    # A caller-sent switch never survives: the switch is the deployment's.
    kwargs = _off(thinking={"type": "enabled"})
    assert mc.apply_thinking_off(kwargs, caps) is True
    assert kwargs.get("reasoning_effort") == effort
    assert kwargs.get("thinking") == thinking


def test_apply_thinking_off_leaves_other_requests_alone():
    kwargs = {"model": "m", "reasoning_effort": "high", "thinking": {"type": "adaptive"}}
    assert mc.apply_thinking_off(kwargs, {mc.THINKING_OFF: "disabled"}) is False
    assert kwargs == {"model": "m", "reasoning_effort": "high", "thinking": {"type": "adaptive"}}
    # Already in the deployment's shape: nothing changes.
    assert mc.apply_thinking_off(_off(thinking={"type": "disabled"}), {mc.THINKING_OFF: "disabled"}) is False
    assert mc.apply_thinking_off(_off(), {}) is False


@pytest.mark.asyncio
async def test_every_routable_shape_maps_to_a_record_key():
    """A merge over a stored record keeps the keys of inconclusive shapes; a routable shape
    the map does not know would have its earlier flag silently erased."""

    async def transient(body):
        raise mc.ProbeCallError("throttled", status=429)

    report = await mc.probe_model("m", transient, supports_reasoning=True)
    routable = {r.shape for r in report.inconclusive if not r.unavoidable}
    assert routable == set(mc.SHAPE_KEYS)
    assert report.inconclusive_keys == {
        mc.FORCED_TOOL_CHOICE,
        mc.RESPONSE_FORMAT,
        mc.THINKING_OFF,
        mc.THINKING_FLOOR,
        mc.THINKING_REPLAY,
    }
