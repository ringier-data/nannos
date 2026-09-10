"""Tests for create_fast_model — the low-latency configuration for utility LLM calls.

`create_model` sends `reasoning_effort` only when a caller passes a `thinking_level`, so the
utility calls that ride the cheap tier sent nothing and inherited the PROVIDER default —
dynamic thinking on the current `chat:low`. That put 2.7-25.5 s (median ~8 s) in front of a
HITL approval card to write one sentence. These tests pin the three properties that fix it:
thinking off, no streaming, and a short output cap.
"""

from unittest.mock import patch

import pytest

from agent_common.core.model_factory import FAST_MODEL_MAX_TOKENS, REASONING_OFF, create_fast_model, create_model


def _captured_kwargs(**kwargs):
    """create_fast_model's delegation to create_model, without building a real client."""
    with patch("agent_common.core.model_factory.create_model") as create:
        create_fast_model("fast-alias", **kwargs)
    assert create.call_args.args == ("fast-alias",)
    return create.call_args.kwargs


def test_thinking_is_off():
    # The whole point: an explicit "none" instead of silence, so the provider default
    # (dynamic thinking) never applies to a one-sentence utility call.
    assert _captured_kwargs()["reasoning_effort"] == REASONING_OFF


def test_streaming_is_off():
    # Every caller awaits the complete answer (structured output, a score, a verdict);
    # a token stream buys nothing and costs a per-chunk callback trip.
    assert _captured_kwargs()["streaming"] is False


def test_output_is_capped_short():
    caps = _captured_kwargs()["max_tokens"]
    assert caps == FAST_MODEL_MAX_TOKENS
    # Bounded well below the reasoning tiers' ceilings: a model ignoring "ONE short
    # sentence" must not be able to stretch the call.
    assert caps <= 2048


def test_caller_can_raise_the_cap():
    assert _captured_kwargs(max_tokens=64)["max_tokens"] == 64


def test_an_empty_effort_is_rejected():
    # "" is not a LiteLLM value: it would take the override branch, then fail the `if effort:`
    # test and send nothing — silently inheriting the provider default this helper exists to
    # override. Fail at the boundary instead.
    with pytest.raises(ValueError, match="REASONING_OFF"):
        create_model("alias", reasoning_effort="")


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_cap_is_rejected(bad):
    # Forwarded verbatim it becomes a gateway 400 or an empty completion, far from the call.
    with pytest.raises(ValueError, match="must be positive"):
        create_model("alias", max_tokens=bad)
