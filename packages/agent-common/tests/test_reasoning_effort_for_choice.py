"""A user's "Extended Thinking off" is sent as off — where the gateway knows how to turn it off.

The orchestrator, sub-agents and scheduled agents used to pass no level for "off", and
`create_model` sends `reasoning_effort` only for a level — so the request carried nothing and
the provider default applied: thinking ON on the Claude 5 family and Gemini 3. The toggle read
"off" and did nothing.

Off is sent only where the alias's probe record says how thinking goes off. Unprobed, the
gateway falls back to its family heuristic, which sends Claude 5.5 a `thinking: disabled` it
rejects (review round 7); where every off request was refused (`unsupported`), sending one is
that refusal. Both keep the provider default, as before any record existed.
"""

import os
from unittest.mock import patch

import pytest

from agent_common.core.model_factory import REASONING_OFF, create_model, reasoning_effort_for_choice
from agent_common.models.base import ThinkingLevel


def _record(thinking_off):
    caps = {} if thinking_off is None else {"thinking_off": thinking_off}
    return patch("agent_common.core.model_factory.get_model_capabilities", return_value=caps)


@pytest.mark.parametrize("way", ["disabled", "between_tools", "none", "always_on"])
def test_no_level_is_off_where_the_record_says_how(way):
    with _record(way):
        assert reasoning_effort_for_choice(None, "m") == REASONING_OFF
        assert reasoning_effort_for_choice("", "m") == REASONING_OFF


@pytest.mark.parametrize("way", [None, "unsupported"], ids=["unprobed", "unsupported"])
def test_no_level_sends_nothing_without_a_usable_record(way):
    """An unprobed claude-*-5-5 (config-defined, registered before the probe, or a failed
    record write) must not get reasoning_effort "none": the hook's heuristic turns it into the
    `thinking: disabled` Claude 5.5 rejects."""
    with _record(way):
        assert reasoning_effort_for_choice(None, "claude-sonnet-5-5") is None


def test_no_model_type_sends_nothing():
    assert reasoning_effort_for_choice(None, None) is None


@pytest.mark.parametrize("level", [ThinkingLevel.low, "high"])
def test_a_level_leaves_the_mapping_to_create_model(level):
    with _record("between_tools"):
        assert reasoning_effort_for_choice(level, "m") is None


@pytest.mark.parametrize(
    "level, record, sent",
    [
        (None, "between_tools", REASONING_OFF),
        (None, None, None),
        (ThinkingLevel.medium, None, "medium"),
    ],
)
def test_the_request_carries_the_choice(level, record, sent):
    with (
        patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://litellm-proxy.test", "LLM_GATEWAY_API_KEY": "sk-test"}),
        patch("agent_common.core.model_factory._gateway_chat_openai_cls") as cls,
        _record(record),
    ):
        create_model("alias", level, reasoning_effort=reasoning_effort_for_choice(level, "alias"), pre_resolved=True)
    assert cls.return_value.call_args.kwargs["model_kwargs"].get("reasoning_effort") == sent
