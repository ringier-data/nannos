"""A user's "Extended Thinking off" is sent as off.

The orchestrator, sub-agents and scheduled agents used to pass no level for "off", and
`create_model` sends `reasoning_effort` only for a level — so the request carried nothing and
the provider default applied: thinking ON on the Claude 5 family and Gemini 3. The toggle read
"off" and did nothing.
"""

import os
from unittest.mock import patch

import pytest

from agent_common.core.model_factory import REASONING_OFF, create_model, reasoning_effort_for_choice
from agent_common.models.base import ThinkingLevel


@pytest.mark.parametrize("level", [None, ""])
def test_no_level_is_off(level):
    assert reasoning_effort_for_choice(level) == REASONING_OFF


@pytest.mark.parametrize("level", [ThinkingLevel.low, "high"])
def test_a_level_leaves_the_mapping_to_create_model(level):
    assert reasoning_effort_for_choice(level) is None


@pytest.mark.parametrize(
    "level, sent",
    [(None, REASONING_OFF), (ThinkingLevel.medium, "medium")],
)
def test_the_request_carries_the_choice(level, sent):
    with (
        patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://litellm-proxy.test", "LLM_GATEWAY_API_KEY": "sk-test"}),
        patch("agent_common.core.model_factory._gateway_chat_openai_cls") as cls,
    ):
        create_model("alias", level, reasoning_effort=reasoning_effort_for_choice(level), pre_resolved=True)
    assert cls.return_value.call_args.kwargs["model_kwargs"]["reasoning_effort"] == sent
